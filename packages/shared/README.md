# @lince/shared — contrato da API

O que trafega para dentro e para fora do control plane é definido **aqui, em um lugar
só**. Duplicar essa definição entre a API e quem a consome é o erro mais caro que dá para
cometer neste projeto: os lados atualizam em ritmos diferentes — o box da loja se atualiza
sozinho por watchtower (§3.8), a API sobe quando alguém faz deploy, o PWA fica no cache do
celular de quem não recarregou — e um campo que diverge só aparece como evento recusado
numa loja, três semanas depois.

São dois contratos, com plateias diferentes:

- **Agente ↔ nuvem (§5).** O caminho crítico: registro, configuração, heartbeat, evento e
  clipe. Uma divergência aqui derruba a detecção de uma loja.
- **Dashboard ↔ nuvem (§4.5).** Sessão, fila de triagem, decisão humana e URL de leitura
  do clipe. Uma divergência aqui trava a triagem — e evento sem triagem não vira
  consequência nenhuma, porque o sistema não decide nada sozinho (§1).

Os schemas são **JSON Schema draft 2020-12**, e não tipos TypeScript. Para o agente isso
é obrigatório: ele é Python. Para o dashboard, que é TypeScript e poderia importar um
tipo, continua valendo o mesmo formato — o custo que se quer evitar não é escrever o tipo
duas vezes, é os dois lados discordarem do contrato sem ninguém perceber, e isso não
depende de falarem a mesma linguagem. O agente valida o payload real contra o schema nos
testes (`apps/agent/tests/test_contrato_evento.py`); a API valida requisição **e**
resposta contra ele em toda rota (`apps/api/src/schemas/dashboard.ts`).

## Arquivos

| Schema | Uso |
|---|---|
| `schemas/common.v1.json` | `$defs` compartilhados: identificadores, instantes, bloco de clipe |
| `schemas/agent-register.v1.json` | Corpo do `POST /v1/agents/register` |
| `schemas/agent-register-response.v1.json` | Resposta do `POST /v1/agents/register` — a credencial em claro aparece só aqui |
| `schemas/event.v1.json` | Corpo do `POST /v1/events` |
| `schemas/event-accepted.v1.json` | Resposta do `POST /v1/events` |
| `schemas/event-clip.v1.json` | Corpo do `PATCH /v1/events/{event_id}` |
| `schemas/config.v1.json` | Corpo do `GET /v1/agents/config` — câmeras, zonas, regras e limiares |
| `schemas/heartbeat.v1.json` | Corpo do `POST /v1/agents/heartbeat` — telemetria periódica, sem identidade nem vídeo |

Do dashboard (§4.5):

| Schema | Uso |
|---|---|
| `schemas/auth-login.v1.json` | Corpo do `POST /v1/auth/login` |
| `schemas/auth-sessao.v1.json` | Resposta do `POST /v1/auth/login` — token e o usuário que ele representa |
| `schemas/usuario.v1.json` | Resposta com um usuário, e os `$defs` de papel e vínculo |
| `schemas/usuarios.v1.json` | Resposta do `GET /v1/usuarios` |
| `schemas/usuario-novo.v1.json` | Corpo do `POST /v1/usuarios` |
| `schemas/usuario-alteracao.v1.json` | Corpo do `PATCH /v1/usuarios/{id}` |
| `schemas/fila-triagem.v1.json` | Resposta do `GET /v1/events` — a fila, e os `$defs` de evento e decisão |
| `schemas/triagem.v1.json` | Corpo do `POST /v1/events/{event_id}/triagem` |
| `schemas/triagem-aceita.v1.json` | Resposta da triagem — o evento como ele passa a aparecer na fila |
| `schemas/clipe-url.v1.json` | Resposta do `GET /v1/events/{event_id}/clip-url` |

Nenhum documento de usuário carrega senha ou hash de senha, em nenhuma direção. O hash
não é público: vazado, vira alvo de força bruta offline — sem rede, sem limite de
tentativa e sem ninguém percebendo.

`config.v1.json` é o único que circula fora de uma resposta HTTP: o agente lê **o mesmo
documento** de um arquivo local (`apps/agent/config.exemplo.json`, `--config`), do
`GET /v1/agents/config` (`--config-nuvem`) e do cache em disco da última configuração
válida (§5.4). Três origens, um formato e um parser — que é o que impede a definição de
existir em dois lugares e divergir. O que é do box e não da loja (URL do Redis, diretório
dos clipes, caminho do `.onnx`, credencial) fica **fora** do documento: repointar o disco
de uma loja não pode ser efeito de uma resposta HTTP.

Três coisas precisam ser verdade nesse endpoint e nenhuma delas está no schema: o `ETag`
tem que ser **estável** para o mesmo conteúdo (senão todo poll baixa o documento
inteiro), tem que **mudar** quando o conteúdo muda, e o `If-None-Match` do agente tem que
ser honrado com `304` sem corpo. É o que `apps/api/src/config/etag.ts` garante.

## Versionamento

- **Major no nome do arquivo e no `$id`.** `event.v1.json` → `event.v2.json`. As duas
  versões convivem, porque o agente e a API atualizam fora de sincronia: a API precisa
  aceitar a versão N−1 enquanto houver loja no ar com a imagem antiga.
- **`schema_version` no payload**, para o servidor rotear sem adivinhar pelo formato.
- **Campo opcional novo não bumpa** a major. Campo obrigatório novo, campo removido ou
  tipo alterado, sim.
- **`additionalProperties: false` na raiz das requisições**, de propósito: obriga a
  extensão a ser explícita e faz a incompatibilidade estourar alto, no primeiro evento,
  em vez de silenciosamente descartar um campo que alguém achou que estava salvando. Nas
  **respostas** ele fica de fora: o servidor pode passar a mandar um campo novo antes de
  o cliente conhecê-lo, e um cliente antigo não pode quebrar por causa disso.

## Regra de instantes

Todo instante é ISO-8601 UTC com **milissegundos** e sufixo `Z`
(`2026-08-12T18:04:11.238Z`). Não é preciosismo de formato: o agente deriva o instante do
evento de um relógio monotônico, e a granularidade honesta do gatilho é limitada pelo GOP
e pela amostragem de 3 fps (~333 ms). Emitir microssegundos seria inventar precisão.
