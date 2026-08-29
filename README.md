# lince

Detecção de situações de possível furto em mercados de bairro, reaproveitando as
câmeras de CFTV já instaladas na loja.

**A borda decide, a nuvem administra.** O vídeo nunca sai da loja — o que sobe é o
evento (JSON) e um clipe de ~15 s. O sistema não decide nada sozinho: todo alerta
passa por triagem humana.

> **Status:** o caminho *pessoa cruza a linha → clipe → fila local → nuvem → clipe no
> bucket* fecha ponta a ponta, sem andaime de gatilho. A borda tem os seis estágios da
> §3.1 à §3.6 ligados por `runtime.py`; a nuvem tem as três rotas de agente (`config`
> com `ETag`/`304`, `register`, `heartbeat`), o caminho do evento (`POST /v1/events`
> com URL pré-assinada e `PATCH /v1/events/{id}`) e o lado humano da §4.5 — login, fila
> de triagem, evento avulso, decisão e URL de leitura do clipe com auditoria.
>
> **O que ainda não existe é a tela:** a triagem é uma API completa que só se alcança
> por `curl`, então nenhum gerente vê um alerta hoje. Isso importa mais do que parece: a
> §7 diz que o maior risco do projeto é a taxa de falso positivo, e medir falso positivo
> exige alguém dizendo que o alerta estava errado — em pé, no corredor, no celular.
> Enquanto o `apps/dashboard` estiver vazio, a regra principal do MVP segue sendo uma
> hipótese não testada. Também não há notificação, nem WebSocket, nem Dockerfile ou o
> compose do box da §3.8, então o agente ainda não é instalável numa loja. Ver
> `CLAUDE.md` para o estado detalhado por componente.
>
> Leia [`docs/arquitetura.md`](docs/arquitetura.md) antes de escrever código, e
> [`CLAUDE.md`](CLAUDE.md) para as convenções — em especial a regra de que toda
> função nova precisa de teste automatizado.

---

## Estrutura

```
apps/api/          API Fastify + Prisma (control plane)
apps/dashboard/    Dashboard React PWA (triagem no celular)
apps/agent/        Agente da borda em Python (YOLO, ByteTrack, ffmpeg)
packages/shared/   Contrato agente ↔ nuvem em JSON Schema (§5 da arquitetura)
infra/             docker-compose de desenvolvimento (Postgres + Redis + MinIO)
docs/              Arquitetura e ADRs
```

## Subir o ambiente

Pré-requisitos: Node 24, pnpm, Python 3.12 e Docker. Já com eles instalados:

```bash
bash scripts/setup.sh              # instala ffmpeg e uv; cria infra/.env
$EDITOR infra/.env                 # troque POSTGRES_PASSWORD (em dois lugares)
pnpm infra:up                      # sobe Postgres, Redis e MinIO
pnpm infra:ps                      # ambos devem aparecer como healthy
```

Comandos auxiliares: `pnpm infra:down` (para), `pnpm infra:logs` (acompanha),
`pnpm infra:reset` (apaga os volumes e recomeça do zero).

## Rodar o agente

Não é preciso ter câmera: `pnpm rtsp:up` sobe câmeras RTSP sintéticas.

```bash
bash scripts/modelo.sh     # modelo .onnx e vídeo com pessoas; nenhum dos dois vai para o git
pnpm rtsp:up
cd apps/agent && uv sync
uv run pytest                                                    # sem rede, sem infra
uv run pytest -m rtsp                                            # exige `pnpm rtsp:up`
uv run pytest -m redis                                           # exige `pnpm infra:up`
uv run pytest -m modelo                                          # exige `scripts/modelo.sh`
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
```

Para ver a detecção e o tracking funcionando, aponte para a `cam3` — a única das três
câmeras sintéticas que publica vídeo com pessoas de verdade, em loop:

```bash
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
  --model models/yolox_s.onnx --dump-tracks ./tracks --duration 45 --stats
```

A linha `[detecção]` mostra o modelo, o *execution provider* que de fato pegou, a
profundidade da fila e, por câmera, a taxa de inferência e quantas caixas saíram. A
`[tracking]` mostra quantas pessoas estão em quadro e `criados_por_minuto`, que é o
sinal de fragmentação.

O `--dump-tracks` grava `tracks/tracks_<câmera>.mp4` com as caixas, os pés e o rastro de
cada pessoa. **É assim que se verifica o estágio 3**: troca de ID não aparece em
contador nenhum, mas no vídeo cada pessoa tem uma cor — se a cor muda no meio do
percurso, houve troca (R-2).

Para ver o caminho inteiro — corte do clipe, fila local e envio — sem uma API do
outro lado:

```bash
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \
  --outbox redis --dry-run --trigger-every 20 --stats
```

Aí o gatilho é andaime — `--trigger-after`, `--trigger-every` e `kill -USR1` — e o que
ele produz sobe marcado como `source: "manual"`, para não contaminar a métrica de falso
positivo por câmera. Ele continua existindo para câmera sem zonas e para teste de
instalação.

Com zonas desenhadas, quem dispara é a regra (§3.4):

```bash
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 --camera-id cam3 \
  --model models/yolox_s.onnx --dry-run --stats --duration 60 \
  --linha-saida 0,400,640,400 --zona-caixa 0,410,260,410,260,478,0,478 \
  --tempo-caixa 3 --vida-minima 1
```

A linha de saída é **orientada**: desenhada da esquerda para a direita, o lado de dentro
da loja fica embaixo (`y` maior), e só cruzar de dentro para fora dispara. Na linha
`[regras]` o que se vigia são os descartes — numa loja de verdade `pagou` tem que
dominar tudo. Perto de zero com eventos subindo é zona de caixa errada; `vida curta`
alto é o tracker fragmentando (R-2).

### A configuração vindo da nuvem (§5.2)

`--config` lê o documento de um arquivo; `--config-nuvem` puxa **o mesmo documento** do
`GET /v1/agents/config` e segue consultando a cada 30 s (ADR-003):

```bash
uv run python -m lince_agent --config-nuvem --api-url http://localhost:3000 \
  --model models/yolox_s.onnx --dry-run --stats
```

O que é da loja desce no documento; o que é do box — `--model`, `--clips-dir`,
`--redis-url`, `--api-url`, `--api-token`, `--config-cache` — continua entrando por
flag. Uma resposta HTTP não repointa o disco de uma loja.

Na subida o agente vai à nuvem e, se ela não responder, usa o **cache local** da última
configuração válida (§5.4) — é o que faz um box reiniciar sem internet em vez de acordar
cego. Sem nuvem e sem cache ele não sobe: um agente sem câmera e sem zona pareceria
saudável no heartbeat e nunca alertaria.

A linha `[config]` do `--stats` mostra o que importa vigiar:

```
[config]   versão=v2 último=14s atrás 304=1 recebidos=1 aplicados=1 reinício-pendente=0 …
```

O número a acompanhar é **`último`**, não a contagem de `304`: com o link caído o poll
falha em silêncio por desenho, e é esse relógio andando que denuncia uma loja rodando
calibração velha. `recebidos` e `aplicados` são propositalmente separados — e
`pendente=` aparecendo significa configuração válida que o agente **não** aplicou:

- **zonas, tempos e limiares** (`rules`, `tracking`, os limiares de `detection`) trocam
  com o agente em pé, e a câmera recalibrada esquece os tracks em curso — tempo de caixa
  medido dentro de um polígono que não existe mais não vale para o novo;
- **mudança estrutural** (câmera entrando ou saindo, `url`, `decode`, `clip`,
  `model_path`) não é aplicada, nem em parte: o agente segue na versão antiga e expõe a
  nova como pendente de reinício, com os campos que a barraram no log. Aplicar só a
  metade quente faria o evento subir declarando um `versions.config` que nunca rodou —
  e é esse campo que vai explicar um falso positivo depois (R-1).

**Primeira vez, ou numa máquina nova?** O passo a passo completo — instalação por
sistema operacional, verificação e troubleshooting — está em
[`docs/setup.md`](docs/setup.md).

## Um aviso sobre hardware

O pipeline da borda precisa de GPU. Num Codespace ou numa máquina sem GPU dá para
desenvolver o control plane inteiro e a estrutura do agente (ingestão, filas,
máquina de estados, contrato com a nuvem), mas **nenhum dos números da §10 da
arquitetura pode ser medido aqui** — taxa de decode, custo de inferência por frame,
latência ponta a ponta e consumo de RAM do buffer circular exigem o box de
referência com vídeo real.
