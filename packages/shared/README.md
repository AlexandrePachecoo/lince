# @lince/shared — contrato agente ↔ nuvem

O que trafega entre a borda e o control plane (§5 da arquitetura) é definido **aqui, em
um lugar só**. Duplicar essa definição entre a API e o agente é o erro mais caro que dá
para cometer neste projeto: os dois lados atualizam em ritmos diferentes — o box da loja
se atualiza sozinho por watchtower (§3.8), a API sobe quando alguém faz deploy — e um
campo que diverge só aparece como evento recusado numa loja, três semanas depois.

Os schemas são **JSON Schema draft 2020-12**, e não tipos TypeScript, porque o agente é
Python e a API é Node. Um formato neutro é a única forma de os dois consumirem a mesma
fonte: o agente valida o payload real contra o schema nos testes
(`apps/agent/tests/test_contrato_evento.py`), e a API gera tipos a partir dele.

## Arquivos

| Schema | Uso |
|---|---|
| `schemas/common.v1.json` | `$defs` compartilhados: identificadores, instantes, bloco de clipe |
| `schemas/event.v1.json` | Corpo do `POST /v1/events` |
| `schemas/event-clip.v1.json` | Corpo do `PATCH /v1/events/{event_id}` |

Reservados, ainda não escritos: `heartbeat.v1.json` (§5.3), `config.v1.json` (§5.2).

## Versionamento

- **Major no nome do arquivo e no `$id`.** `event.v1.json` → `event.v2.json`. As duas
  versões convivem, porque o agente e a API atualizam fora de sincronia: a API precisa
  aceitar a versão N−1 enquanto houver loja no ar com a imagem antiga.
- **`schema_version` no payload**, para o servidor rotear sem adivinhar pelo formato.
- **Campo opcional novo não bumpa** a major. Campo obrigatório novo, campo removido ou
  tipo alterado, sim.
- **`additionalProperties: false` na raiz**, de propósito: obriga a extensão a ser
  explícita e faz a incompatibilidade estourar alto, no primeiro evento, em vez de
  silenciosamente descartar um campo que alguém achou que estava salvando.

## Regra de instantes

Todo instante é ISO-8601 UTC com **milissegundos** e sufixo `Z`
(`2026-08-12T18:04:11.238Z`). Não é preciosismo de formato: o agente deriva o instante do
evento de um relógio monotônico, e a granularidade honesta do gatilho é limitada pelo GOP
e pela amostragem de 3 fps (~333 ms). Emitir microssegundos seria inventar precisão.
