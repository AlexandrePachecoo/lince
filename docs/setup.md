# Rodando o lince na sua máquina

Passo a passo para deixar o ambiente de desenvolvimento funcionando do zero.
Tempo esperado: ~15 minutos, quase tudo download.

> **O que este guia entrega:** Postgres e Redis rodando, as ferramentas da borda
> instaladas e o agente funcionando contra câmeras sintéticas — ingestão (§3.1),
> corte do clipe (§3.5) e fila local com envio (§3.6). API e dashboard ainda não
> existem, então o envio é exercitado com `--dry-run`.

---

## Antes de começar: você tem GPU?

O pipeline da borda (§3 da [arquitetura](arquitetura.md)) faz inferência YOLO sobre
6–10 câmeras simultâneas. Isso pede GPU NVIDIA.

| Sem GPU | Com GPU NVIDIA |
|---|---|
| Control plane inteiro: API, banco, workers, dashboard | Tudo da coluna ao lado |
| Estrutura do agente: ingestão, filas, máquina de estados, contrato com a nuvem | Pipeline de detecção em taxa real |
| Detecção em CPU, poucos fps, vídeo sintético | Benchmarks da §10 da arquitetura |

Sem GPU você não fica bloqueado — a maior parte do trabalho do v1 é control plane.
O que você **não** consegue é medir: taxa de decode, custo de inferência por frame,
latência ponta a ponta e consumo de RAM do buffer circular. Esses números só saem
do box de referência com vídeo real, e até lá seguem como hipótese no documento.

---

## 1. Pré-requisitos

| Ferramenta | Versão mínima | Por quê |
|---|---|---|
| Node.js | 24 | API Fastify, dashboard, workers |
| pnpm | 10 | gerenciador do monorepo |
| Python | 3.12 | agente da borda (YOLO, ByteTrack) |
| Docker + Compose v2 | — | Postgres, Redis, containers do agente |
| ffmpeg | 6 | decode RTSP e corte do clipe |
| git | — | — |

`ffmpeg` e `uv` são instalados pelo `scripts/setup.sh` do passo 3. Os demais você
instala antes.

### Linux (Ubuntu / Debian)

```bash
# Node 24 via nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
exec $SHELL
nvm install 24 && nvm use 24
corepack enable pnpm

# Python 3.12 e git
sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv git

# Docker Engine — siga o guia oficial da sua distro:
# https://docs.docker.com/engine/install/
sudo usermod -aG docker $USER   # depois faça logout/login
```

### macOS

```bash
# Homebrew, se ainda não tiver: https://brew.sh
brew install node@24 python@3.12 git
corepack enable pnpm

# Docker Desktop (ou Colima, se preferir sem GUI)
brew install --cask docker
```

Abra o Docker Desktop uma vez antes de seguir — ele precisa estar rodando.

> **Nota sobre macOS e a borda:** Apple Silicon não tem CUDA. O control plane roda
> perfeitamente; o pipeline de detecção, não em condições reais.

### Windows

Use **WSL2 com Ubuntu**, não o Windows nativo. O agente depende de ffmpeg, Docker e
(em produção) drivers NVIDIA num ambiente Linux — desenvolver fora disso cria
diferenças que só aparecem no deploy.

```powershell
wsl --install -d Ubuntu
```

Instale o Docker Desktop no Windows com a integração WSL2 ativada, abra o Ubuntu e
siga as instruções de **Linux** acima. Mantenha o repositório dentro do sistema de
arquivos do WSL (`~/projetos/lince`), nunca em `/mnt/c/...` — I/O em `/mnt/c` é
lento a ponto de atrapalhar.

### Atalho: GitHub Codespaces

Abrindo o repositório num Codespace, o `.devcontainer` cuida de tudo. Pule direto
para o passo 3 — Node, pnpm, Python e Docker já vêm prontos.

---

## 2. Clonar

```bash
git clone https://github.com/AlexandrePachecoo/lince.git
cd lince
```

## 3. Rodar o setup

```bash
bash scripts/setup.sh
```

O script é idempotente — pode rodar quantas vezes quiser. Ele:

1. instala **ffmpeg** (apt, brew ou dnf, conforme o sistema);
2. instala **uv**, o gerenciador de dependências Python do agente;
3. roda `pnpm install` se já houver algum pacote no workspace;
4. cria `infra/.env` a partir de `infra/.env.example`.

Se o `uv` não for encontrado no comando seguinte, abra um terminal novo — o
instalador adiciona `~/.local/bin` ao PATH e a sessão atual não enxerga isso.

## 4. Configurar o `.env`

```bash
$EDITOR infra/.env
```

Troque `POSTGRES_PASSWORD` por algo real. **Atenção:** a senha aparece em dois
lugares no arquivo — na variável `POSTGRES_PASSWORD` e dentro da `DATABASE_URL`.
Elas precisam bater, senão o Postgres sobe e o Prisma não conecta.

Para gerar uma senha e aplicar nos dois de uma vez:

```bash
sed -i "s/troque-esta-senha/$(openssl rand -hex 16)/g" infra/.env
```

No macOS o `sed` pede um argumento a mais: `sed -i ''` no lugar de `sed -i`.

`infra/.env` está no `.gitignore` e nunca deve ser commitado.

## 5. Subir a infraestrutura

```bash
pnpm infra:up
```

Sobe Postgres 16 e Redis 7 em containers, com volumes nomeados — os dados
sobrevivem a `infra:down` e a reinícios da máquina.

---

## 6. Verificar

Rode os seis comandos. Todos devem passar antes de você considerar o ambiente
pronto.

```bash
ffmpeg -version                        # → ffmpeg version 6.x
uv --version                           # → uv 0.x
pnpm infra:ps                          # → postgres e redis, ambos (healthy)
docker compose -f infra/docker-compose.yml exec postgres pg_isready -U lince
                                       # → accepting connections
docker compose -f infra/docker-compose.yml exec redis redis-cli ping
                                       # → PONG
git status --short                     # → infra/.env NÃO pode aparecer
```

O `pnpm infra:ps` pode levar uns 10 segundos para sair de `starting` e chegar em
`healthy` na primeira vez. Se continuar em `starting` depois de um minuto, veja o
troubleshooting.

---

## Comandos do dia a dia

| Comando | O que faz |
|---|---|
| `pnpm infra:up` | sobe Postgres e Redis |
| `pnpm infra:ps` | mostra o estado dos containers |
| `pnpm infra:logs` | acompanha os logs (Ctrl+C sai) |
| `pnpm infra:down` | para os containers, **preserva** os dados |
| `pnpm infra:reset` | para e **apaga os volumes** — banco zerado |
| `pnpm rtsp:up` | sobe as câmeras RTSP sintéticas (ver abaixo) |
| `pnpm rtsp:down` | derruba as câmeras |
| `pnpm rtsp:logs` | logs do MediaMTX e dos publishers |
| `uv run pytest -m redis` | testes da fila local; exige `pnpm infra:up` |
| `bash scripts/setup.sh` | reexecuta o setup; seguro a qualquer momento |

---

## Rodando o agente da borda

Três estágios existem e estão ligados: **ingestão** (§3.1, um `ffmpeg` por câmera
lendo RTSP), **clipe** (§3.5, buffer circular de 30 s em RAM e corte em `-c copy`) e
**fila local com envio** (§3.6, Redis + `POST /v1/events`, `PUT` do clipe e `PATCH`).
Falta o miolo: YOLO, tracking e o motor de regras (§3.2 a §3.4).

Como nada dispara evento sozinho sem o motor de regras, o gatilho é andaime:
`--trigger-after S`, `--trigger-every S` e `kill -USR1 <pid>`.

Você não precisa de câmera nenhuma para trabalhar nele. `pnpm rtsp:up` sobe um
servidor RTSP local com duas câmeras sintéticas em 640x480 a 15 fps:

| Câmera | URL | Para quê |
|---|---|---|
| cam1 | `rtsp://localhost:8554/cam1` | keyframe a cada 2 s — a câmera de referência |
| cam2 | `rtsp://localhost:8554/cam2` | keyframe a cada 10 s — câmera mal configurada |

```bash
pnpm rtsp:up
cd apps/agent
uv sync
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
```

A saída mostra, por segundo: estado da câmera, frames entregues e a taxa efetiva
(deve estabilizar em 3,00 fps), fragmentos acumulados e há quanto tempo chegou o
último frame. `Ctrl+C` encerra.

### Vendo o caminho inteiro até a nuvem

```bash
pnpm infra:up          # o Redis daqui serve de fila local em desenvolvimento
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \
  --outbox redis --redis-url redis://localhost:6379/0 \
  --dry-run --trigger-every 20 --stats
```

Uma segunda linha de status aparece, com a fila: quantos eventos e clipes estão
pendentes, a idade do mais antigo e quantos já subiram. Vinte segundos depois do
primeiro gatilho você vê a sequência `POST → PUT → PATCH` no log e o diretório de
clipes voltar a ficar vazio — é o NFR-3 acontecendo: o único arquivo de vídeo do
sistema é temporário.

O `--dry-run` aceita tudo sem falar com ninguém. Sem ele, o agente aponta para
`--api-url` de verdade, que ainda não existe: a fila vai encher, e é justamente
assim que dá para ver o comportamento de link caído.

Sem Redis à mão, `--outbox memory` roda tudo em RAM — mas ele avisa alto que não é
durável, porque um restart perde os eventos que não subiram.

### Testes

```bash
cd apps/agent
uv run pytest              # unitários; não precisam de rede, câmera nem infra
uv run pytest -m rtsp      # ponta a ponta; exige `pnpm rtsp:up`
uv run pytest -m redis     # fila durável; exige `pnpm infra:up`
```

Os marcados são os únicos que dependem de infraestrutura, e cada um cobre o que só
existe em execução. Os de `rtsp` provam que os dois pipes do ffmpeg fluem ao mesmo
tempo sem travar um ao outro e que os fragmentos formam um MP4 reproduzível. Os de
`redis` provam o que uma reimplementação em memória poderia modelar diferente:
atomicidade da reserva de item, ordenação por score e sobrevivência do estado ao
processo. A mesma suíte de contrato roda nas duas filas, e é isso que impede uma de
ganhar correção que a outra não recebe.

Por padrão os testes usam o banco 15 (`LINCE_REDIS_URL`, default
`redis://localhost:6379/15`), com prefixo próprio por teste e sem nunca dar
`FLUSHDB` — sua stack de desenvolvimento no banco 0 fica intacta.

### Simulando as falhas do §3.1

As duas falhas de câmera que a arquitetura prevê saem dos containers:

```bash
docker stop lince-cam1     # câmera offline: o ffmpeg morre
                           # → reconnecting, backoff crescente, depois offline
docker start lince-cam1    # → volta para ok sozinho

docker pause lince-cam1    # stream travado com o socket vivo
                           # → só o watchdog pega; o -timeout do ffmpeg não
docker unpause lince-cam1
```

A segunda é a que engana e a razão de existir um watchdog: o socket continua
aberto, o ffmpeg não reclama, e nenhum frame sai.

---

## Troubleshooting

| Sintoma | Causa provável | Solução |
|---|---|---|
| `POSTGRES_PASSWORD is required` ao subir | `infra/.env` não existe | `cp infra/.env.example infra/.env` e edite |
| `bind: address already in use` na 5432 | Postgres já roda na máquina | mude `POSTGRES_PORT` em `infra/.env` (ex.: 5433) e ajuste a `DATABASE_URL` |
| Mesmo erro na 6379 | Redis já roda na máquina | mesma coisa com `REDIS_PORT` |
| `permission denied` no socket do Docker (Linux) | usuário fora do grupo `docker` | `sudo usermod -aG docker $USER`, depois logout/login |
| `uv: command not found` logo após o setup | PATH da sessão atual | abra um terminal novo, ou `export PATH="$HOME/.local/bin:$PATH"` |
| Postgres preso em `starting` | volume corrompido de uma tentativa anterior | `pnpm infra:reset` e suba de novo (**apaga os dados**) |
| Prisma não conecta, mas o container está `healthy` | senha diferente entre `POSTGRES_PASSWORD` e `DATABASE_URL` | acerte as duas e rode `pnpm infra:reset` — a senha do Postgres só é aplicada na criação do volume |
| `ffmpeg` instalado mas sem NVDEC | build sem suporte a CUDA | irrelevante sem GPU; no box de referência use um build com `--enable-cuda-nvcc` |
| `uv run pytest -m rtsp` pula tudo | câmeras sintéticas não estão de pé | `pnpm rtsp:up` e espere uns 5 s |
| `uv run pytest -m redis` pula tudo | Redis fora do ar ou em outra porta | `pnpm infra:up`, ou aponte `LINCE_REDIS_URL` |
| Agente com `fila local recusou o evento` | Redis local fora do ar | é a fronteira de durabilidade do ADR-004: o evento se perde e o contador sobe. Suba o Redis ou use `--outbox memory` |
| Agente em `reconnecting` com `404 Not Found` | o MediaMTX está no ar mas nenhum publisher está publicando naquele caminho | `pnpm rtsp:ps` — o container `lince-cam1` precisa estar `Up` |
| `bind: address already in use` na 8554 | outro servidor RTSP na máquina | mude `RTSP_PORT` em `infra/.env` |

---

## Estrutura do repositório

```
apps/api/          API Fastify + Prisma (control plane)   — vazio
apps/dashboard/    Dashboard React PWA (triagem)          — vazio
apps/agent/        Agente da borda em Python              — estágios 1, 5 e 6
  src/lince_agent/ffmpeg/    montagem do comando, processo, pipes, parser fMP4
  src/lince_agent/ingest/    supervisão: reconexão, watchdog, saúde
  src/lince_agent/clip/      buffer circular em RAM, corte, teto de disco
  src/lince_agent/outbox/    fila local, política de retry, cliente HTTP, envio
  src/lince_agent/runtime.py composição: é aqui que os estágios viram um processo
packages/shared/   Contrato agente ↔ nuvem em JSON Schema (§5)
infra/             docker-compose de desenvolvimento e das câmeras sintéticas
scripts/setup.sh   bootstrap idempotente
docs/              arquitetura e ADRs
```

Os diretórios vazios existem de propósito: a fronteira entre borda e nuvem está
desenhada desde o começo. `packages/shared` é onde o contrato da §5 (payload de
evento, heartbeat, formato de configuração) mora em um lugar só — duplicar essa
definição entre a API e o agente é o erro mais caro que dá para cometer neste
projeto. Os schemas são JSON Schema, e não tipos TypeScript, porque o agente é
Python: um formato neutro é o que permite aos dois lados consumirem a mesma fonte.

## Próximo passo

Leia [`arquitetura.md`](arquitetura.md) inteiro antes de escrever código. Em
especial a §7 (riscos): o maior risco do projeto não é a arquitetura, é a taxa de
falso positivo — e isso muda como cada decisão de implementação deve ser tomada.
