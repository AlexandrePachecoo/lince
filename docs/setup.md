# Rodando o lince na sua máquina

Passo a passo para deixar o ambiente de desenvolvimento funcionando do zero.
Tempo esperado: ~15 minutos, quase tudo download.

> **O que este guia entrega:** Postgres e Redis rodando e as ferramentas da borda
> instaladas. Ainda **não existe código de aplicação** no repositório — nem API,
> nem dashboard, nem agente. Este é o ambiente onde eles vão ser escritos.

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
| `bash scripts/setup.sh` | reexecuta o setup; seguro a qualquer momento |

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

---

## Estrutura do repositório

```
apps/api/          API Fastify + Prisma (control plane)   — vazio
apps/dashboard/    Dashboard React PWA (triagem)          — vazio
apps/agent/        Agente da borda em Python              — vazio
packages/shared/   Contrato agente ↔ nuvem (§5)           — vazio
infra/             docker-compose de desenvolvimento
scripts/setup.sh   bootstrap idempotente
docs/              arquitetura e ADRs
```

Os diretórios vazios existem de propósito: a fronteira entre borda e nuvem está
desenhada desde o começo. Em particular, `packages/shared` é onde o contrato da §5
(payload de evento, heartbeat, formato de configuração) vai morar em um lugar só —
duplicar essa definição entre a API e o agente é o erro mais caro que dá para
cometer neste projeto.

## Próximo passo

Leia [`arquitetura.md`](arquitetura.md) inteiro antes de escrever código. Em
especial a §7 (riscos): o maior risco do projeto não é a arquitetura, é a taxa de
falso positivo — e isso muda como cada decisão de implementação deve ser tomada.
