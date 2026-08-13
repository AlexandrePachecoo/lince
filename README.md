# lince

Detecção de situações de possível furto em mercados de bairro, reaproveitando as
câmeras de CFTV já instaladas na loja.

**A borda decide, a nuvem administra.** O vídeo nunca sai da loja — o que sobe é o
evento (JSON) e um clipe de ~15 s. O sistema não decide nada sozinho: todo alerta
passa por triagem humana.

> **Status:** agente com os estágios 1 (ingestão RTSP, §3.1), 5 (clipe, §3.5) e 6
> (fila local e envio, §3.6) ligados ponta a ponta — o caminho gatilho → clipe →
> fila → nuvem funciona, com gatilho de andaime no lugar do motor de regras.
> Detecção, tracking, regras, API e dashboard ainda não existem.
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
infra/             docker-compose de desenvolvimento (Postgres + Redis)
docs/              Arquitetura e ADRs
```

## Subir o ambiente

Pré-requisitos: Node 24, pnpm, Python 3.12 e Docker. Já com eles instalados:

```bash
bash scripts/setup.sh              # instala ffmpeg e uv; cria infra/.env
$EDITOR infra/.env                 # troque POSTGRES_PASSWORD (em dois lugares)
pnpm infra:up                      # sobe Postgres e Redis
pnpm infra:ps                      # ambos devem aparecer como healthy
```

Comandos auxiliares: `pnpm infra:down` (para), `pnpm infra:logs` (acompanha),
`pnpm infra:reset` (apaga os volumes e recomeça do zero).

## Rodar o agente

Não é preciso ter câmera: `pnpm rtsp:up` sobe câmeras RTSP sintéticas.

```bash
pnpm rtsp:up
cd apps/agent && uv sync
uv run pytest                                                    # sem rede, sem infra
uv run pytest -m rtsp                                            # exige `pnpm rtsp:up`
uv run pytest -m redis                                           # exige `pnpm infra:up`
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
```

Para ver o caminho inteiro — corte do clipe, fila local e envio — sem uma API do
outro lado:

```bash
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \
  --outbox redis --dry-run --trigger-every 20 --stats
```

O gatilho é andaime: o motor de regras (§3.4) ainda não existe, então `--trigger-after`,
`--trigger-every` e `kill -USR1` ocupam o lugar dele. Os eventos que eles produzem
sobem marcados como `source: "manual"`.

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
