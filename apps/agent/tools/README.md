# Exportar o RTMPose-s e medir a pose

O agente não carrega torch, mmpose nem rtmlib. O `.onnx` também não entra no
git. Estes dois scripts rodam na máquina do fundador, com o arquivo fora do
repositório (`~/lince-modelos` no Linux, `D:\lince-modelos` no Windows).

Os dois checkpoints aceitos são Apache-2.0 e têm **17 juntas COCO**:

- `coco` — `rtmpose-s_8xb256-420e_coco-256x192`
- `body8` — o COCO-17 do metafile `rtmpose_body8-coco` (o arquivo publicado se
  chama `body7`; não é o `body8-halpe26`)

WholeBody e Halpe são recusados pelo nome. Ultralytics também.

## Exportar

O export usa um venv à parte e o mmdeploy
(`pose-detection_simcc_onnxruntime_dynamic.py`, `export_postprocess=False`).
O ONNX sai com lote dinâmico e SimCC cru. A normalização continua no agente
(`pose/preprocess.py`), não no grafo. Torch de CPU chega: a placa não entra
nesta etapa.

Linux:

```bash
python3 apps/agent/tools/exporta_rtmpose.py \
  --destino "$HOME/lince-modelos/rtmpose-s.onnx" \
  --variante coco
```

Windows (PowerShell):

```powershell
py -3.12 apps\agent\tools\exporta_rtmpose.py `
  --destino D:\lince-modelos\rtmpose-s.onnx `
  --variante coco
```

`--dry-run` mostra o plano e não baixa nada. `--variante body8` troca o
checkpoint. O YOLOX do detector continua sendo o de `bash scripts/modelo.sh`
(no Windows, o mesmo URL do script, com `curl`).

## Benchmark na GPU

No ambiente do agente, um grupo de cada vez. Os dois juntos se sobrescrevem e
o que sobra importa sem erro:

```bash
cd apps/agent
uv sync --no-group cpu --group gpu
```

`onnxruntime-gpu` a partir da 1.27 pede CUDA 13, driver recente e placa com
compute capability ≥ 7.5. Sem a placa, `--provider cpu`.

Linux:

```bash
cd apps/agent
uv run python tools/bench_pose.py ../../infra/videos/pessoas.mp4 \
  --detector models/yolox_s.onnx \
  --pose "$HOME/lince-modelos/rtmpose-s.onnx" \
  --provider cuda \
  --saida "$HOME/lince-modelos/bench_pose.json"
```

Windows:

```powershell
cd apps\agent
uv run python tools\bench_pose.py D:\videos\pessoas.mp4 `
  --detector models\yolox_s.onnx `
  --pose D:\lince-modelos\rtmpose-s.onnx `
  --provider cuda `
  --saida D:\lince-modelos\bench_pose.json
```

`--provider cpu` força `CPUExecutionProvider`. Com `cuda`, a lista é
`CUDAExecutionProvider` e depois `CPUExecutionProvider`: se a placa não subir,
a sessão cai para CPU e o JSON grava o provider que rodou.

A tabela traz p50/p95 em ms, por pessoa e por frame, com 1, 3 e 6 pessoas:
recorte NumPy, inferência e decode. Um frame com menos gente repete as caixas
até N — o número é o do lote. O `cv2.warpAffine` só aparece se o opencv já
estiver instalado nesse ambiente; se não estiver, o script avisa e segue. Não
instale opencv no `pyproject.toml` do agente por causa desta coluna.

ffmpeg e ffprobe precisam estar no PATH (é o que lê o vídeo). O JSON não deve
voltar para o git.
