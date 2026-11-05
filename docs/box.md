# O box da loja — instalação e operação

O box é o desktop com GPU que fica no estoque do mercado e roda o agente da borda (§3
da [arquitetura](arquitetura.md)). Este documento é o passo a passo de quem vai montar
um, do zero, e o que olhar depois que ele estiver de pé.

Do repositório, o box recebe **três coisas e nada mais**:

```
/opt/lince/
├── docker-compose.yml     ← infra/box/docker-compose.yml
├── .env                   ← a partir de infra/box/.env.example
└── modelos/
    └── yolox_s.onnx
```

A imagem do agente vem pronta do registry (`ghcr.io/alexandrepachecoo/lince-agent`),
publicada pelo CI a cada tag `agente-vX.Y.Z`. O box não compila nada e não tem o código.

---

## 1. Hardware

| Item | Mínimo | Por quê |
|---|---|---|
| GPU | NVIDIA com *compute capability* **7.5 ou mais** — GTX 16xx, RTX 20xx em diante | O `onnxruntime-gpu` da imagem é compilado contra CUDA 13, que não gera código para Maxwell, Pascal e Volta. **GTX 10xx usada não serve**, por mais barata que esteja |
| Driver NVIDIA | **580 ou mais novo** | Exigência do CUDA 13. Com driver mais velho o provider de CUDA não carrega e a inferência roda em CPU sem erro (§10.10) |
| RAM | 16 GB | O buffer circular de 30 s fica em RAM, por câmera (§3.5). O número real por câmera é pendência da §10.8 |
| Disco | SSD, 20 GB livres para o Lince | A imagem carrega CUDA e cuDNN e passa de alguns GB; o resto é folga para clipes esperando a internet voltar |
| Rede | cabo na mesma rede do DVR/câmeras | Wi-Fi no box é a forma mais barata de transformar R-13 em rotina |
| Energia | nobreak | R-6. Um apagão derruba as câmeras de qualquer jeito, mas a oscilação que não derruba as câmeras derruba um PC sem nobreak |

E um ajuste na BIOS que ninguém lembra até precisar: **ligar sozinho quando a energia
volta** (*Restore on AC Power Loss* → *Power On*). Sem isso, toda queda de luz vira uma
visita à loja para apertar o botão.

## 2. Sistema

Ubuntu 24.04 LTS. Depois da instalação limpa:

```bash
# driver NVIDIA (o ubuntu-drivers escolhe o recomendado; confira que é >= 580)
sudo ubuntu-drivers install
sudo reboot
nvidia-smi                       # tem que listar a placa e "Driver Version: 580..." ou maior

# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"  # logout/login depois disto

# NVIDIA Container Toolkit — é o que dá ao container acesso à placa
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

## 3. Acesso remoto (Tailscale, no host)

Tailscale vai instalado **no sistema, não em container**. É a porta de entrada para
consertar o box à distância (R-6), e se morasse no compose um Docker quebrado levaria
junto justamente o acesso que serviria para consertá-lo.

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --ssh --hostname "lince-<loja>"
```

`--ssh` liga o Tailscale SSH: dá para entrar no box pela tailnet sem abrir porta nenhuma
no roteador da loja, que é o que mercado de bairro nunca vai ter (§3.8).

## 4. O Lince

```bash
sudo mkdir -p /opt/lince/modelos && sudo chown "$USER" /opt/lince /opt/lince/modelos
cd /opt/lince

# os dois arquivos do repositório (do commit/tag que a loja vai rodar)
curl -fsSLO https://raw.githubusercontent.com/AlexandrePachecoo/lince/main/infra/box/docker-compose.yml
curl -fsSL  https://raw.githubusercontent.com/AlexandrePachecoo/lince/main/infra/box/.env.example -o .env
chmod 600 .env
$EDITOR .env        # LINCE_VERSAO, LINCE_API_URL, LINCE_AGENT_TOKEN

# o modelo, conferido pelo checksum — o mesmo do scripts/modelo.sh
curl -fL -o modelos/yolox_s.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx
echo "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063  modelos/yolox_s.onnx" \
  | sha256sum -c
```

Se o pacote da imagem no GitHub estiver **privado**, o box precisa de login no registry
com um token de leitura (`read:packages`):

```bash
echo "<token>" | docker login ghcr.io -u alexandrepachecoo --password-stdin
```

A imagem não carrega credencial nem modelo, e o código já é público; tornar o pacote
público (Settings do pacote no GitHub → *Change visibility*) dispensa esse passo em
todo box.

O token do agente (`LINCE_AGENT_TOKEN`) hoje é provisionado à mão: o
`POST /v1/agents/register` existe na API, mas o lado do agente que troca o token de
bootstrap pela credencial ainda não (§5.1).

### Subir

```bash
docker compose up -d
docker compose ps                  # redis "healthy", agente "running"
docker compose logs -f agente
```

O agente sobe, vai à nuvem buscar a configuração da loja e começa a puxar as câmeras.
Sem nuvem e sem cache local ele **não sobe** — de propósito (§5.4): um agente sem câmera
pareceria saudável no heartbeat e nunca alertaria.

## 5. Conferir que a placa está sendo usada

Esta é a parte que não se pode pular. Quase tudo que dá errado com GPU **não dá erro**:
o agente cai para CPU como foi desenhado para cair (§3.1, §3.2), e a loja passa a operar
a uma fração da taxa sem ninguém perceber.

```bash
# a inferência: o log de subida diz qual provider do ONNX Runtime pegou
docker compose logs agente | grep "carregado em"     # "modelo ... carregado em CUDAExecutionProvider"

# o decode: a coluna "dec" sobe quando o NVDEC está trabalhando
nvidia-smi dmon -s u -c 5

# os processos na placa: o python do agente e um ffmpeg por câmera
nvidia-smi
```

O decode por hardware é **pedido por câmera** na configuração que vem da nuvem
(`decode.hwaccel: "cuda"`, `packages/shared/schemas/config.v1.json`). O padrão é
`none`: câmera cadastrada sem isso decodifica em CPU mesmo num box com placa.

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| `could not select device driver "nvidia"` no `up` | NVIDIA Container Toolkit ausente ou Docker não reiniciado | refaça o fim da seção 2 |
| provider é `CPUExecutionProvider` | driver < 580, ou placa anterior à série 16 | `nvidia-smi`; ver a tabela da seção 1 |
| `dec` em 0% com câmeras rodando | câmera sem `decode.hwaccel: "cuda"` na configuração | ajuste no cadastro da câmera |
| `dec` em 0% e `hwaccel` pedido | `video` fora das capacidades do container | não remova `video` do compose nem do Dockerfile; `tests/test_box.py` guarda os dois |
| agente reiniciando em loop | sem nuvem **e** sem cache, ou modelo fora de `modelos/` | `docker compose logs agente` diz qual |

## 6. Atualizar e voltar atrás

Cada loja roda a versão que o `.env` dela diz — nunca `latest` (R-7). Atualizar é trocar
o número:

```bash
cd /opt/lince
sed -i 's/^LINCE_VERSAO=.*/LINCE_VERSAO=0.2.0/' .env
docker compose pull agente && docker compose up -d agente
docker compose logs -f agente      # e repetir a seção 5
```

Voltar atrás é o mesmo comando com o número anterior. A fila local e os clipes estão em
volumes e sobrevivem às duas direções: o que esperava a internet continua esperando.

Atualização automática ficou de fora por enquanto. O Watchtower, que a §3.8 previa, saiu
de manutenção em dezembro de 2025 — e de qualquer forma atualizava sem conferir a saúde
nem voltar atrás, que é o que o R-7 pede. O substituto é um script próprio (puxar,
subir, conferir que o agente fica de pé, voltar à versão anterior se não ficar), e ele
depende de o agente expor um sinal de saúde local que hoje não existe.

---

## Na máquina de desenvolvimento com GPU

O mesmo compose, com a imagem construída do código local e o agente na rede do host —
para alcançar a API do `pnpm dev` e as câmeras do `pnpm rtsp:up` em `localhost`:

```bash
cd infra/box
cat > .env <<'ENV'
LINCE_VERSAO=dev
LINCE_API_URL=http://localhost:3000
LINCE_AGENT_TOKEN=dev-agent-token-local-only
LINCE_MODELOS_DIR=../../apps/agent/models
ENV
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

O porquê de cada diferença está no cabeçalho de `infra/box/docker-compose.dev.yml`.

Sem Docker, direto pelo `uv`, a GPU pede o outro grupo de dependências — o padrão é o
ONNX Runtime de CPU, e os dois não convivem no mesmo ambiente (`pyproject.toml`):

```bash
cd apps/agent
uv sync --no-group cpu --group gpu
uv run --no-group cpu --group gpu python -m lince_agent ...   # o `uv run` puro voltaria para o grupo cpu
```
