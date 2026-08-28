# lince — orientações para trabalhar neste repositório

Detecção de possível furto em mercados de bairro, reaproveitando o CFTV existente.
O sistema **não decide nada sozinho**: produz um alerta com clipe curto e um humano
confirma ou descarta.

Leia [`docs/arquitetura.md`](docs/arquitetura.md) antes de escrever código —
especialmente a §7 (riscos). O maior risco do projeto não é a arquitetura, é a taxa
de falso positivo, e isso muda como cada decisão de implementação deve ser tomada.

Princípio organizador: **a borda decide, a nuvem administra.** Vídeo nunca sai da
loja; o que sobe é o evento (JSON) e o clipe de ~15 s.

---

## Toda função nova precisa de teste automatizado

**Obrigatório, sem exceção.** Código novo sem teste não está pronto, e não importa
se parece trivial. Isto vale para função, método, branch de decisão e correção de
bug.

O motivo é específico deste projeto: quase tudo aqui é **assíncrono, multi-thread e
dependente de processo externo**. Um bug de ingestão não estoura — ele entrega
frames embaralhados, perde um segundo de vídeo em silêncio ou trava o ffmpeg inteiro
sem log nenhum. Bug assim não aparece rodando na mão; aparece três semanas depois,
numa loja, como falso positivo que ninguém consegue explicar.

Regras práticas:

1. **Correção de bug começa pelo teste que falha.** Se o teste não falha antes da
   correção, ele não está testando o bug.
2. **Teste o comportamento e o porquê, não a implementação.** O docstring do teste
   deve dizer o que quebra no mundo real se aquilo regredir — veja
   `tests/test_process.py` como referência de tom.
3. **Nada de mock onde cabe o real.** ffmpeg roda no ambiente; use-o. Mock fica para
   o que é caro ou indisponível: GPU, rede instável, saída de `-hwaccels` num
   servidor sem placa.
4. **Se testar está difícil, o desenho está errado.** Foi assim que
   `CameraSupervisor` ganhou `ingest_factory` e `clock` injetáveis — sem isso, a
   máquina de estados só era testável esperando segundos por transição.
5. **Teste que depende de infraestrutura leva marcador.** Hoje só existe
   `@pytest.mark.rtsp`, excluído por padrão. O resto tem que rodar com
   `uv run pytest` numa máquina limpa.
6. **Nunca use `sleep` para sincronizar teste.** Use relógio falso, `Event` ou
   polling com prazo. Teste que depende de tempo real fica instável em CI.
7. **Não construa falha de E/S sobre bit de permissão.** O agente roda como root no
   container (§3.8) e o CI também: `chmod(0o500)` não impede o uid 0 de escrever, e o
   teste passa na máquina do dev e falha exatamente nos dois lugares onde precisava
   valer. Provoque o `OSError` por estrutura — caminho cujo pai é um arquivo, por
   exemplo —, que falha para qualquer usuário.

Antes de dar qualquer coisa por concluída:

```bash
cd apps/agent && uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest
```

---

## Estado atual

| Componente | Situação |
|---|---|
| `apps/agent` | Estágios 1 a 6 (§3.1 a §3.6) ligados por `runtime.py`. N câmeras com zonas próprias, vindas do `GET /v1/agents/config` com `ETag`/`304`, poll de 30 s, cache local e troca a quente. Heartbeat da §5.3 sobe a cada 30 s |
| `apps/api` | As três rotas de agente (`config`, `register`, `heartbeat`), o caminho do evento (`POST /v1/events` com URL pré-assinada e `PATCH /v1/events/{event_id}`) e o lado humano da §4.5: login, fila de triagem, decisão, URL de leitura do clipe com auditoria e cadastro de usuário. Falta download de modelo, notificação e WebSocket |
| `apps/dashboard` | vazio — a API que ele consome já existe inteira |
| `packages/shared` | JSON Schema dos dois contratos: agente ↔ nuvem (§5) e dashboard ↔ nuvem (§4.5) |

O caminho **pessoa cruza a linha → clipe → fila local → nuvem → clipe apagado do
disco** funciona ponta a ponta, sem andaime nenhum e **sem `--dry-run`**: a borda vê,
dá identidade e **decide**, e a nuvem do outro lado aceita, devolve a URL pré-assinada
e recebe a confirmação do upload. Os eventos sobem com `source: "rule"` e o bloco
`rule` preenchido.

O andaime de gatilho (`--trigger-after`, `--trigger-every`, `SIGUSR1`) continua
existindo para câmera sem zonas desenhadas e para teste de instalação, e o que ele
produz sobe como `source: "manual"` — de propósito, para não contaminar a métrica de
falso positivo por câmera (R-1).

As zonas já não são digitadas na linha de comando, e já não vêm só de arquivo. Há duas
fontes do mesmo documento da §5.2 (`packages/shared/schemas/config.v1.json`, exemplo em
`apps/agent/config.exemplo.json`): `--config ARQUIVO` e `--config-nuvem`, que puxa do
`GET /v1/agents/config` e segue consultando a cada 30 s. As duas passam pelo **mesmo**
`config_loader.monta_config`; nada abaixo do `AgentConfig` sabe de onde veio. As flags de
zona continuam existindo para apontar o pipeline para uma câmera e olhar o que sai, e são
recusadas junto com as duas em vez de ignoradas.

O transporte da configuração está inteiro e é onde mora a decisão mais fácil de errar do
§5.2, a de **o que dá para trocar com o agente em pé**:

- `config_diff.py` classifica cada campo como `A_QUENTE` ou `ESTRUTURAL`, e
  `test_config_diff.py` falha se um campo novo ficar sem classificação. Isso não é zelo:
  campo sem classificação é campo ignorado pelo diff, e ignorado pelo diff significa
  *trocado a quente sem ninguém reiniciar nada* — o lado errado do erro.
- **Tudo ou nada por documento.** Um campo estrutural diferente e nada é aplicado, nem a
  metade quente. O evento sobe com `versions.config`, e um agente rodando "a v7 com as
  câmeras da v6" declararia v7: a investigação de um falso positivo partiria de uma
  calibração que nunca existiu (R-1).
- Recalibrar uma câmera **zera os tracks em curso dela**, e só dela. O tempo acumulado no
  caixa foi medido dentro de um polígono que não existe mais. Custa segundos de cegueira
  numa câmera; o outro lado seria um evento medido metade em cada calibração.
- Os limiares de tracking, ao contrário, trocam **sem** zerar nada: são limiares de
  associação, não estado, e o Kalman de quem está andando continua válido.
- O cache local (§5.4) guarda o **documento cru**, não o `AgentConfig` — um terceiro
  formato seria o menos testado dos três. E o que o cache resolve não é o poll em regime,
  é a subida: box que reinicia com o link caído.

O que separa o documento do que é do box é a divisão da §5.2, e ela é dura: o que desce
da nuvem são **quais** limiares; onde o `.onnx` pousou, qual Redis, qual API e qual
credencial são da máquina e entram por flag. Uma resposta HTTP não pode repointar o
disco de uma loja.

`apps/api` já serve o §5 inteiro menos o modelo — ver
[`apps/api/prisma/schema.prisma`](apps/api/prisma/schema.prisma) e
[`apps/api/src/routes/`](apps/api/src/routes/). Decisões que valem registrar porque a
próxima rota vai bater nelas de novo:

- `config_version`/`ETag` **não são coluna nenhuma no Postgres** — são hash SHA-256 do
  documento canônico (`apps/api/src/config/canonical-json.ts`), calculado sob demanda a
  cada `GET`. Uma coluna exigiria lembrar de bumpá-la em todo caminho de escrita futuro;
  hash sob demanda é correto por construção. `ETag` e `config_version` são o mesmo valor
  — uma fonte, não duas rotinas de hash para manter sincronizadas.
- Câmera é **JSONB por câmera** (`decode`/`supervision`/`clip`/`rules`), não tabelas
  `ZONA`/`REGRA_CONFIG` normalizadas do §6 — decisão desta fatia, para quando o
  dashboard existir e precisar editar zona por zona num formulário.
- Autenticação é por hash SHA-256 em `Agente.tokenHash` — nunca o token em claro, nem em
  seed de dev. O `POST /v1/agents/register` já troca um token de bootstrap de uso único
  (consumo atômico por `UPDATE ... WHERE usado_em IS NULL`, que é o que resiste a duas
  chamadas concorrentes) por essa credencial; o token semeado do `scripts/seed.ts`
  continua existindo só para o atalho de desenvolvimento.
- O documento que a API monta é validado contra
  `packages/shared/schemas/config.v1.json` via Ajv antes de responder — defesa em
  profundidade, o par do lado da API do que `tests/test_contrato_config.py` garante do
  lado do agente. Uma falha aí vira `500` (bug interno), nunca um `200` malformado.

O caminho do evento (`POST /v1/events` → `PUT` no bucket → `PATCH /v1/events/{id}`) é
onde a idempotência do §5.4 deixa de ser conversa e vira código. As decisões que
sustentam isso, e que são fáceis de desfazer sem perceber:

- **O `event_id` é a chave primária da tabela**, não uma coluna única ao lado de um id
  gerado na nuvem. A idempotência passa a ser do banco: dois `POST` do mesmo evento não
  têm como virar duas linhas, e o gerente não vê o mesmo furto duas vezes na fila.
- **A chave do objeto é determinística** (`storage/object-key.ts`), derivada de
  `tenant_id`, do dia de `occurred_at` e do `event_id`. Não é economia de coluna: o
  agente reposta o evento justamente para renovar uma URL vencida, e depois de um `404`
  no `PATCH` ele repõe o evento **sem** reenviar os bytes — contando com a chave
  estável. Chave nova a cada emissão órfãria o que já subiu e aponta o evento para um
  objeto que ninguém escreveu. A data sai de `occurred_at` e nunca do relógio do
  servidor, senão um evento das 23h59 reenviado às 00h02 ganha duas chaves.
- **Reenvio não reescreve nada do evento** (`update: {}` no upsert). O bloco `clip` do
  payload é congelado na borda no corte e continua dizendo `ok` — o desfecho do
  **corte** — muito depois de o `PATCH` ter registrado o **upload**. Reescrevê-lo
  rebaixaria para `pendente` um clipe que já está no bucket, e a triagem ficaria
  esperando para sempre um upload que já aconteceu. Quem manda no desfecho do clipe é o
  `PATCH`, sozinho.
- **`clipeEstado` é um terceiro estado, e não o `clip.status` do contrato.** `pendente`
  é o que o contrato não tem nome para: a nuvem conhece o evento, emitiu a URL, e os
  bytes ainda não chegaram. Sem ele não dá para distinguir "o vídeo está subindo" de
  "não vai haver vídeo" — e é a segunda que libera o triador a decidir sem esperar.
- **Escopo errado é `403` e evento desconhecido é `404`, e os dois números são
  combinados com `classify_response`.** `400` mandaria o evento para a fila morta: um
  box provisionado com o tenant errado perderia o dia inteiro da loja enquanto ninguém
  conserta o cadastro. `404` no `PATCH` não significa "não achei", significa "reponha o
  evento" — inclusive para evento de outro tenant, que assim não vaza existência e
  ainda leva o agente a fazer a coisa certa.
- **`camera_id` é texto sem FK.** Um evento pode subir seis horas depois e chegar com a
  câmera já removida do cadastro; com FK, a API recusaria e o agente jogaria fora um
  alerta real por causa de uma edição no dashboard.
- **A `object_key` do `PATCH` é conciliação, não entrada.** A API emitiu a URL, então
  já sabe a chave; gravar a do cliente deixaria uma credencial de loja apontar o evento
  para qualquer objeto do bucket. Divergência vira log.

A §4.5 fechou o ciclo do produto: o alerta que a borda decide agora chega a um humano que
confirma ou descarta, que é o que o §1 exige antes de qualquer consequência. As decisões
desta fatia, e o que cada uma custa se for desfeita sem perceber:

- **Credencial humana e credencial de agente são caminhos disjuntos.** As duas leem o
  mesmo `Authorization: Bearer` e não se cruzam em lugar nenhum: `autenticaUsuario`
  verifica um JWT, `autenticaAgente` procura um hash de token. Nada decide "que tipo de
  credencial é esta" — a rota já sabe quem ela atende. O token do agente vive num box no
  estoque de um mercado; se ele abrisse a fila, o clipe de qualquer evento estaria a um
  arrombamento de distância.
- **JWT HS256 escrito à mão (`auth/jwt.ts`)**, ao contrário de `clip-storage.ts`, que
  preferiu `aws4fetch` a reimplementar SigV4. A diferença é a superfície: SigV4 é grande e
  errar é silencioso; um JWT com um emissor, um segredo e um algoritmo é um HMAC sobre
  duas strings. O que faz CVE em biblioteca de JWT é a generalidade que este uso não tem —
  e aqui o cabeçalho do token **não escolhe nada**, porque só é lido depois de o HMAC
  fechar. Inverter essa ordem é a família inteira de furos de `alg`.
- **Senha é scrypt com sal, não o SHA-256 dos tokens** (`auth/senha.ts`). Token é 256 bits
  de `randomBytes`; senha é escolhida por gente e cabe em dicionário. Os parâmetros do KDF
  viajam dentro do hash, para subir o custo um dia sem trancar todo mundo para fora.
- **O JWT poupa a tabela de sessão, não o `SELECT`.** Papel e lojas vêm do banco a cada
  requisição — precisam vir, senão tirar um gerente de uma loja só valeria quando a sessão
  dele vencesse. Como a leitura acontece de qualquer jeito, `ativo` e `tokenVersao` são
  conferidos ali: é isso que faz desativar alguém e trocar senha valerem **agora**.
- **Triagem é append-only e re-triagem é permitida.** Mudar de ideia grava linha nova; a
  vigente é a última, por `seq` e não por `criadoEm` (empate de milissegundo tornaria
  "vigente" cara ou coroa). O motivo é o dedo errado: NFR-9 pede dois toques, com o
  celular numa mão, num corredor. Decisão imutável transformaria um toque errado em falso
  positivo permanente na estatística da câmera — o número do R-1.
- **A fila pagina por cursor, nunca por OFFSET.** Ela cresce por cima enquanto alguém a
  percorre, e com OFFSET cada evento novo empurra um antigo para uma página já lida: some
  da tela sem ninguém decidir nada. Evento sem triagem é dívida operacional **visível**
  (§4.5), e uma paginação que esconde eventos é a forma mais fácil de invisibilizá-la.
- **A auditoria grava a emissão da URL, não o download.** Depois que a URL sai, o GET vai
  direto ao bucket e não passa pela API — é o preço de o vídeo nunca atravessar o control
  plane (§4.4). A emissão é o único instante em que a nuvem sabe quem pediu, e por isso a
  linha é escrita **antes** de assinar: errar para o lado de auditar demais é o único lado
  aceitável (R-8).
- **`admin` é papel de loja, não de tenant.** Administrar gente exige admin em **todas** as
  lojas envolvidas — inclusive nas que o alvo já tem, senão o admin da loja A desativaria
  alguém que também trabalha na loja B, a partir de uma tela onde a loja B não aparece.
  Não há papel de tenant: um segundo eixo de permissão é o que se confere errado no
  primeiro caso de canto.
- **Dentro do tenant a recusa é `403`; fora dele é `404`.** Quem está autenticado já sabe
  que as lojas da rede existem, e "peça acesso ao admin" é acionável. Confirmar que um
  `event_id` ou um e-mail existe em **outra** rede já é vazamento.

Onde encostar em cada coisa:

- **Dashboard (§4.5)** é o `apps/dashboard` vazio. A API que ele consome já existe
  inteira, e o contrato dela está em `packages/shared/schemas` — a régua é NFR-9: um toque
  abre o evento com o vídeo rodando, o segundo é a decisão.
- **Registro do agente no lado do agente (§5.1)** é o outro lado do
  `POST /v1/agents/register`, que já existe na API: trocar um token de bootstrap por
  credencial na subida, em vez do `--api-token` semeado.
- **Download de modelo (§5.2)** substitui o caminho local em `DetectionOptions.model_path`.
  O `model_version` já é nome + checksum, no formato que o ADR-006 quer.
- **Métrica de falso positivo por câmera/dia (R-1)** agora tem de onde sair: é a triagem
  vigente agrupada por câmera e dia, com `source = rule` separado de `manual`. É a leitura
  que justifica tudo o que veio antes, e ainda não existe.

O heartbeat (§5.3) é `heartbeat.py`, e o que ele tem de particular não é o envio — é o
que ele **não** faz. Não há fila: um envio que falhou não deixa nada para trás, porque a
única fotografia que interessa é a de agora, e uma fila entregaria à nuvem, depois de
uma noite offline, duzentas fotografias do passado. Também não passa por
`classify_response` nem por `classify_config_response`: as duas existem para decidir o
que fazer com um item de fila, e aqui o status só escolhe qual contador sobe. O
`clock_skew_s` sai do cabeçalho `Date` da resposta **anterior** — é o único relógio
externo que o box tem, e sem ele um agente com NTP quebrado reportaria deriva zero com
toda a convicção do mundo.

`packages/shared` é onde o contrato da §5 mora em **um lugar só**. Duplicar essa
definição entre a API e o agente é o erro mais caro que dá para cometer neste
projeto. O agente valida o payload real contra o schema em
`tests/test_contrato_evento.py` — é esse teste que impede a divergência.

## Comandos

```bash
pnpm infra:up                 # Postgres, Redis e MinIO (papel do R2 em desenvolvimento)
bash scripts/modelo.sh        # modelo .onnx e vídeo com pessoas (fora do git)
pnpm rtsp:up                  # câmeras RTSP sintéticas (MediaMTX)

cd apps/agent
uv sync
uv run pytest                 # padrão: sem rede, sem câmera, sem infra, sem modelo
uv run pytest -m rtsp         # ponta a ponta; exige `pnpm rtsp:up`
uv run pytest -m redis        # fila local durável; exige `pnpm infra:up`
uv run pytest -m modelo       # o .onnx de verdade; exige `bash scripts/modelo.sh`
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats

# a loja inteira a partir do documento da §5.2 — mesmo formato que a nuvem vai servir.
# O caminho do modelo é do box, não do documento, e por isso continua sendo flag:
uv run python -m lince_agent --config config.exemplo.json \
  --model models/yolox_s.onnx --dry-run --stats

# o mesmo documento, agora puxado da nuvem a cada 30 s (§5.2), com cache local para
# reiniciar sem internet (§5.4). `--config`, `--config-nuvem` e `--camera` se excluem:
uv run python -m lince_agent --config-nuvem --api-url http://localhost:3000 \
  --model models/yolox_s.onnx --dry-run --stats

# detecção e tracking contra vídeo com pessoas de verdade (cam3):
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
  --model models/yolox_s.onnx --stats

# a verificação do estágio 3 é visual: cada pessoa mantém uma cor no vídeo gravado,
# e cor trocando no meio do percurso é troca de ID (R-2)
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
  --model models/yolox_s.onnx --dump-tracks ./tracks --duration 45 --stats

# caminho inteiro até a nuvem, sem uma API do outro lado:
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \
  --outbox redis --dry-run --trigger-every 20 --stats

# estágio 4: a borda decidindo sozinha, sem andaime de gatilho. A linha é orientada —
# desenhada da esquerda para a direita, o lado de dentro da loja fica embaixo (y maior):
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 --camera-id cam3 \
  --model models/yolox_s.onnx --dry-run --stats --duration 60 \
  --linha-saida 0,400,640,400 --zona-caixa 0,410,260,410,260,478,0,478 \
  --tempo-caixa 3 --vida-minima 1
```

```bash
cd apps/api
cp .env.example .env           # senha já bate com infra/.env por padrão
pnpm install
pnpm migrate                   # aplica prisma/migrations contra DATABASE_URL
DATABASE_URL=$TEST_DATABASE_URL pnpm exec prisma migrate deploy   # só na 1ª vez, schema de teste
pnpm test                      # Postgres e MinIO reais (TEST_*), nada mockado
pnpm lint
pnpm seed                      # tenant/loja/agente/câmeras de dev, espelha config.exemplo.json
pnpm dev                       # sobe em :3000

# a mesma loja semeada, agora puxada pela API de verdade (em vez de --config):
cd ../agent && uv run python -m lince_agent --config-nuvem --api-url http://localhost:3000 \
  --api-token dev-agent-token-local-only --model models/yolox_s.onnx --dry-run --stats

# e o caminho inteiro, sem --dry-run: o evento sobe, o clipe vai direto para o bucket
# por URL pré-assinada e o PATCH confirma. `enviados=` e `clipes=` na linha [fila] com
# `falhas=0` é o sinal de que os três passos fecharam:
cd ../agent && uv run python -m lince_agent --config-nuvem --api-url http://localhost:3000 \
  --api-token dev-agent-token-local-only --model models/yolox_s.onnx --stats
```

O outro lado, o do §4.5 — login, fila de triagem, decisão e clipe. O `pnpm seed` imprime
este bloco já preenchido, com o usuário e a senha de desenvolvimento:

```bash
cd apps/api
TOKEN=$(curl -s -X POST http://localhost:3000/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"gerente@loja-dev.local","senha":"senha-de-desenvolvimento"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')

# a fila: por padrão só o que falta decidir, do mais recente para o mais antigo
curl -s "http://localhost:3000/v1/events?limite=5" -H "authorization: Bearer $TOKEN"

# a URL assinada do clipe (5 min), que vai direto no <video> — e gera linha de auditoria
curl -s http://localhost:3000/v1/events/<EVENT_ID>/clip-url -H "authorization: Bearer $TOKEN"

# a decisão. Repetir com outra decisão é correção, não erro: grava linha nova
curl -s -X POST http://localhost:3000/v1/events/<EVENT_ID>/triagem \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"decisao":"falso_positivo","observacao":"cliente pagou no autoatendimento"}'
```

Na fila o que se vigia é o que **não** foi decidido: `triagem=pendentes` é o padrão, e uma
fila que só cresce é a dívida operacional que o §4.5 quer visível. `clip_state=pendente`
com o evento velho é upload que nunca chegou — outro problema, e o log do agente é onde
ele aparece. E `clip_state` não é `clip.status`: o primeiro é o desfecho do **upload**, o
segundo o do **corte** na borda.

O que aterrissou no bucket, para conferir na mão o que a linha `[fila]` afirma — pelo
console do MinIO em `http://localhost:9001` (credencial em `infra/.env`) ou por `mc`:

```bash
set -a && . infra/.env && set +a
docker run --rm --network lince-dev_default \
  -e MC_HOST_local="http://$MINIO_ROOT_USER:$MINIO_ROOT_PASSWORD@minio:9000" \
  minio/mc ls --recursive local/"$S3_BUCKET"
```

Na linha `[regras]` o que se vigia são os **descartes**, não os eventos: numa loja de
verdade `pagou` tem que dominar tudo. Perto de zero com eventos subindo é zona de caixa
errada; `vida curta` alto é o tracker fragmentando (R-2), e aí mexer no N não adianta.

Na linha `[config]` o que se vigia é a **idade do último sucesso**, não a contagem de
`304`: com o link caído o poll falha em silêncio por desenho (§5.4), e é esse relógio
andando que denuncia uma loja rodando calibração velha. `pendente=` aparecendo é
configuração válida que exige reinício, e o log diz quais campos a barraram.

Na linha `[heartbeat]` vale a mesma leitura, pelo mesmo motivo: o que denuncia uma loja
que a nuvem parou de enxergar é `último=` envelhecendo além dos 30 s, não a contagem de
envios — que zera no reinício, justamente o evento que se quer detectar. `inválidos=`
diferente de zero é outra coisa e manda depurar noutro lugar: não é rede, é o corpo
divergindo do `heartbeat.v1.json`. E `skew=?` não é `skew=+0.0s` — o primeiro é "nunca
houve resposta com `Date`", o segundo é "os relógios batem".

## Convenções

- **Código, comentário, docstring, teste e commit em português.** Nomes de
  biblioteca e termos técnicos consagrados ficam como são (`framerate`, `keyframe`,
  `backoff`).
- **Comentário explica o porquê, não o quê.** Se o código diz o quê, o comentário
  diz por que aquela escolha e o que ela custa. Referencie a seção da arquitetura
  (`§3.5`, `R-9`, `NFR-2`) quando a decisão vem de lá.
- Python 3.12 com `uv`, `ruff` para lint e formatação, linha de 100.
- Node 24 com `pnpm`.
- Toda entidade com dado de tenant carrega `tenant_id`, e toda query filtra por ele
  (NFR-6).

## Armadilhas já pagas

Não reintroduza nenhuma destas — cada uma custou depuração e tem teste guardando:

- **`-reconnect` não funciona em RTSP.** É opção de HTTP; passá-la numa entrada
  `rtsp://` não dá erro e não faz nada. Reconexão é código Python.
- **`-fflags nobuffer` e `-use_wallclock_as_timestamps` só valem para fonte ao
  vivo.** Em arquivo não dão erro — perdem frames em silêncio.
- **Todo pipe do ffmpeg tem que ser drenado por uma thread própria.** Um pipe cheio
  bloqueia o processo inteiro, inclusive as outras saídas. Consumidor lento nunca
  pode virar contrapressão: a fila entre leitura e consumo descarta o mais antigo.
- **`-frag_duration` junto com `+frag_keyframe`** produz fragmento que começa no
  meio do GOP e quebra o descarte do buffer circular.
- **`rawvideo` não tem framing.** O tamanho do frame é contrato; errá-lo embaralha
  tudo sem levantar erro.
- **`HTTPError` do `urllib` é levantado _e_ é a resposta.** Tem `.code`, `.headers` e
  `.read()`. Tratá-lo só como exceção joga fora o status, e é o status que decide
  entre reenviar e mandar para a fila morta.
- **`PUT` de arquivo sem `Content-Length` vira `Transfer-Encoding: chunked`**, que o
  R2 recusa numa URL pré-assinada. E o corpo precisa ser **reaberto** a cada
  tentativa: um arquivo já lido está no fim, e o reenvio sobe zero byte sem erro.
- **Score de ZSET 0.0 é falsy.** `if not zscore(...)` tira da fila um item agendado
  para o instante zero, em silêncio. Sempre `is None`.
- **Teste de fila com a nuvem aceitando é corrida.** O sender drena entre o gatilho e
  a asserção. Nos testes de fila o link fica caído por padrão; quem quer o caminho
  completo pede o cliente que aceita, explicitamente.
- **Cada família de modelo tem sua própria convenção de entrada.** YOLOX preenche o
  letterbox no canto **superior esquerdo**, consome **BGR** e recebe **0..255**; a
  linhagem YOLOv5/v8 centraliza, quer RGB e 0..1. Errar qualquer uma das três não dá
  erro: o modelo devolve caixas plausíveis e deslocadas, e o sintoma aparece no §3.4
  como zona errada. É por isso que `PreprocessSpec` é dado explícito e que existe o
  teste de ida e volta em `test_detect_preprocess.py`.
- **A saída do YOLOX não são caixas.** São deslocamentos relativos a uma grade de
  âncoras, com `wh` em log. Sem somar a grade e exponenciar, as caixas saem coladas no
  canto superior esquerdo — e o número de âncoras tem que bater com o `input_size`,
  senão a decodificação é silenciosamente absurda.
- **`np.clip(a[:, [0, 2]], …, out=a[:, [0, 2]])` não escreve nada.** Indexação por
  lista produz cópia; o `out=` aponta para um temporário e o recorte se perde em
  silêncio. Fatia com passo (`a[:, 0::2]`) é vista, e a atribuição funciona.
- **União zero na IoU vira `nan`, e `nan <= limiar` é falso.** A caixa degenerada
  sobrevive a toda supressão, sempre e sem sinal.
- **Vídeo de teste com pessoas não é detalhe.** `testsrc2` é padrão de barras: um teste
  de detecção contra ele concorda com qualquer coisa, inclusive com um detector
  quebrado. A `cam3` existe para isso.
- **A 3 fps, os parâmetros de tracking das implementações de referência não servem.**
  Elas assumem 30 fps. Duas consequências, ambas medidas: o `dt` do Kalman tem que ser
  tempo real (a fila do §3.2 descarta frames, então o intervalo varia), e os limiares de
  IoU precisam ser bem mais frouxos — uma pessoa andando tem IoU de **0,19** entre caixas
  cruas consecutivas. Copiar o ruído de processo diagonal da referência também quebra: ou
  o filtro fica lento demais, ou o ruído de posição explica todo o movimento e a
  velocidade estimada vai a zero.
- **O estado do Kalman não tem restrição, e a caixa vira do avesso.** Quem se afasta da
  câmera encolhe, o filtro aprende altura negativa, e extrapolar durante uma oclusão de
  segundos produz `y2 < y1`. Nada estoura: a IoU dá zero, o desenho some, e a
  `base_central` aponta para o lugar errado. Só apareceu em vídeo real.
- **A caixa de um `Track` não é recortada na moldura** — ao contrário da caixa de uma
  `Detection`. Ela é estimativa, e quem sai pela porta tem os pés estimados além da
  borda; recortar empurraria toda travessia para a beirada do quadro.
- **Amostra exatamente sobre a linha de saída não pode virar a nova referência.** Um
  ponto com `lado == 0` não tem lado, então a travessia é adiada para o frame seguinte —
  mas se ele substituir o ponto anterior, a comparação seguinte parte de novo de um ponto
  sem lado e devolve "não cruzou" para sempre. A pessoa atravessa a porta e o motor nunca
  vê: sem log, sem contador, sem rastro. É a falha mais perigosa do §3.4, porque um
  evento que não acontece não deixa nada para depurar.
- **Zona desenhada sobre um frame de outra resolução não levanta erro.** Ela
  simplesmente não contém ninguém: o tempo de caixa fica em zero e a câmera passa a
  alertar para todo cliente que sai. Por isso `CameraConfig` recusa na subida qualquer
  vértice fora de `decode.width/height`.
- **Track `PERDIDO` é extrapolação do Kalman, não observação.** Decidir travessia sobre
  ele manda o gerente abrir um clipe em que ninguém atravessa nada. O §3.4 só amostra
  track com detecção nova no frame (`time_since_update_s == 0`); a oclusão vira um
  segmento mais comprido entre duas observações reais.
- **Na reconexão da câmera os IDs de track recomeçam do 1.** Quem guarda estado por
  `track_id` — o §3.4 guarda — tem que zerar junto com o `TrackerPool`, senão a primeira
  pessoa da sessão nova herda o desfecho de outra e nunca mais alerta.
- **`urllib` levanta `HTTPError` para o `304` também.** No poll de configuração o `304`
  é o caminho **saudável** e o mais frequente de todos — 2 880 vezes por dia, por loja.
  Tratá-lo como exceção põe o agente em backoff exponencial por estar tudo bem, e a loja
  para de receber calibração sem um erro em lugar nenhum.
- **`classify_response` não serve ao poll de configuração.** Ela é do envio de evento e
  diverge em dois pontos que doem: `304` cairia em `RETRY`, e `404` significa "evento
  desconhecido, reposte" no envio e "esta nuvem não conhece este agente" no poll. Por
  isso existe `classify_config_response`, e por isso ela tem teste comparando as duas.
- **`200` sem corpo não é configuração.** É proxy, redirecionamento capturado ou API meio
  implantada. Passá-lo ao loader contaria erro de contrato num problema de rede e mandaria
  procurar o bug no documento em vez de no caminho até ele.
- **Documento inválido não pode avançar o `ETag` nem entrar no cache.** Avançar o `ETag`
  faria a nuvem responder `304` para um documento que este agente nunca conseguiu ler — e
  o conserto, publicado em seguida, viria como `304` também. Cachear é pior: vira a
  configuração de subida do próximo boot sem rede, e aí a loja não sobe mais.
- **`200` sem `ETag` tem que *esquecer* o anterior.** Manter o antigo pede um `304` que
  significaria "você ainda tem a v1" quando o agente já está na v2, e o poll para de
  enxergar toda mudança seguinte. Falha permanente e sem sintoma.
- **Contador de transporte não pode dizer "aplicado".** O poller não sabe se o runtime
  aplicou: documento estrutural é recebido, validado, cacheado e **recusado**. Por isso
  `ConfigPollerStats.recebidos` e `ConfigHealth.aplicacoes` são números diferentes, lado
  a lado no `--stats`.
- **`pkill -f` casa com a própria linha de comando do shell que o executa.** Não é bug do
  projeto, mas custou uma depuração aqui: um `pkill -f "tmp/nuvem.py"` dentro de um script
  que menciona esse caminho mata o próprio script, e o sintoma é um comando que "sai com
  144" sem log nenhum.
- **Detecção fraca nunca cria track.** O piso do detector é 0,10 para alimentar a segunda
  passada do ByteTrack, e é a regra "só continua track existente" que impede isso de
  virar falso positivo.
- **URL pré-assinada não pode assinar `Content-Type` nem `Content-Length`.** Só `host`
  entra no `X-Amz-SignedHeaders`. Assiná-los obrigaria o agente a mandar exatamente o
  mesmo valor que a API previu, e qualquer divergência viraria um `403` do bucket — que
  se parece com credencial errada e manda depurar no lugar errado.
- **O bloco `clip` do `POST` é o desfecho do corte; o do `PATCH` é o do upload.** São
  campos com o mesmo nome e significados diferentes. Tratá-los como um só faz o reenvio
  do evento rebaixar para `pendente` um clipe que já está no bucket, e a triagem passa a
  esperar para sempre um upload que já aconteceu.
- **URL assinada contra bucket que não existe funciona.** A assinatura sai perfeita e o
  erro só aparece no `PUT`, como `404`, parecendo problema de credencial. É por isso que
  o `pnpm infra:up` cria os buckets num container próprio em vez de contar com a
  primeira escrita.
- **`format: "email"` estoura o Ajv em `strict`.** Formato desconhecido não é ignorado: a
  compilação do schema falha, e como os validadores são compilados na importação do
  módulo, o sintoma é a API inteira não subir. Ou entra `ajv-formats`, ou o schema usa
  `pattern` — que é o que se fez, porque e-mail válido de verdade só se prova mandando
  mensagem, e regex severa recusa endereço legítimo de cliente.
- **`addSchema` e depois `compile` do mesmo arquivo duplica o `$id`.** `compile` já
  registra. Fazer os dois estoura com "schema with key or id ... already exists" na
  subida. O que resolve é compilar na ordem das dependências: quem é referenciado
  primeiro, e `addSchema` só para o que nunca é raiz (`common.v1.json`).
- **Normalizar o e-mail depois de validar recusa quem digitou certo.** O teclado do
  celular capitaliza a primeira letra e o autocompletar deixa espaço no fim. Validando
  antes, `" Ana@Loja.local "` vira `400` "formato inválido" sobre um endereço
  visivelmente correto. A ordem é normalizar e **então** validar (`auth/email.ts`), e a
  mesma função tem que servir ao login e ao cadastro — se divergirem, nasce conta que
  nenhum login alcança.
- **Ordenar a triagem vigente por `criadoEm` é cara ou coroa.** Duas decisões no mesmo
  milissegundo empatam, e "a última" passa a depender de como o Postgres devolveu a
  linha. Por isso existe `Triagem.seq`: a sequência não empata.
