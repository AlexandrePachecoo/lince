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

Antes de dar qualquer coisa por concluída:

```bash
cd apps/agent && uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest
```

---

## Estado atual

| Componente | Situação |
|---|---|
| `apps/agent` | Estágios 1 a 6 (§3.1 a §3.6) ligados por `runtime.py`. N câmeras com zonas próprias, vindas do `GET /v1/agents/config` com `ETag`/`304`, poll de 30 s, cache local e troca a quente |
| `apps/api` | vazio |
| `apps/dashboard` | vazio |
| `packages/shared` | JSON Schema do evento, do PATCH do clipe e da configuração (§5). Heartbeat ainda não |

O caminho **pessoa cruza a linha → clipe → fila local → nuvem → clipe apagado do
disco** funciona ponta a ponta, sem andaime nenhum: a borda vê, dá identidade e
**decide**. Os eventos sobem com `source: "rule"` e o bloco `rule` preenchido.

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

Onde encostar em cada coisa:

- **Configuração (§5.2)** está fechada do lado do agente. O que falta é o outro lado:
  o `GET /v1/agents/config` de verdade, servido pela API, com `ETag` estável e o
  `config_version` valendo alguma coisa. Quem impede os dois lados de divergirem é
  `tests/test_contrato_config.py`; quem impede o agente de aplicar o que não pode é
  `tests/test_config_diff.py`.
- **Heartbeat (§5.3)** já tem os dados reunidos em `AgentHealth`, agora incluindo
  `inference_fps` e `dropped_frames` por câmera e os contadores de descarte do §3.4;
  falta o transporte, que depende do registro da §5.1.
- **Download de modelo (§5.2)** substitui o caminho local em `DetectionOptions.model_path`.
  O `model_version` já é nome + checksum, no formato que o ADR-006 quer.

`packages/shared` é onde o contrato da §5 mora em **um lugar só**. Duplicar essa
definição entre a API e o agente é o erro mais caro que dá para cometer neste
projeto. O agente valida o payload real contra o schema em
`tests/test_contrato_evento.py` — é esse teste que impede a divergência.

## Comandos

```bash
pnpm infra:up                 # Postgres e Redis
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

Na linha `[regras]` o que se vigia são os **descartes**, não os eventos: numa loja de
verdade `pagou` tem que dominar tudo. Perto de zero com eventos subindo é zona de caixa
errada; `vida curta` alto é o tracker fragmentando (R-2), e aí mexer no N não adianta.

Na linha `[config]` o que se vigia é a **idade do último sucesso**, não a contagem de
`304`: com o link caído o poll falha em silêncio por desenho (§5.4), e é esse relógio
andando que denuncia uma loja rodando calibração velha. `pendente=` aparecendo é
configuração válida que exige reinício, e o log diz quais campos a barraram.

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
