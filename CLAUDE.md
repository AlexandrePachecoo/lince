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
| `apps/agent` | Estágio 1 (ingestão RTSP, §3.1). Sem YOLO, tracking, regras ou clipe |
| `apps/api` | vazio |
| `apps/dashboard` | vazio |
| `packages/shared` | vazio — vai abrigar o contrato agente↔nuvem da §5 |

`packages/shared` é onde o contrato da §5 (payload de evento, heartbeat, formato de
configuração) mora em **um lugar só**. Duplicar essa definição entre a API e o
agente é o erro mais caro que dá para cometer neste projeto.

## Comandos

```bash
pnpm infra:up                 # Postgres e Redis
pnpm rtsp:up                  # câmeras RTSP sintéticas (MediaMTX)

cd apps/agent
uv sync
uv run pytest                 # padrão: sem rede, sem câmera
uv run pytest -m rtsp         # ponta a ponta; exige `pnpm rtsp:up`
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
```

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
