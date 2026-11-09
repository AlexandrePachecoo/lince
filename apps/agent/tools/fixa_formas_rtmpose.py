"""Grava no ONNX do RTMPose-s as formas que o grafo de fato devolve.

O `pose-detection_simcc_onnxruntime_dynamic.py` do mmdeploy declara o eixo das
juntas como simbólico (`MatMulsimcc_x_dim_1`), não como 17. A sessão roda
igual, mas `OnnxPose` recusa na subida: sem o 17 declarado, um WholeBody de 133
canais renomeado passaria e o pulso sairia no índice errado, sem erro. A trava
fica como está; este passo é o que o export faz depois do mmdeploy.

Não carimba às cegas. Roda o grafo no CPU com um lote de 2, confere que as
duas saídas são (2, 17, 384) e (2, 17, 512), e só então grava 17 e os bins no
arquivo. O lote continua `batch`: o 2 é só o tamanho da observação.

Roda no venv do export, que é quem tem `onnx` — o agente não carrega essa
dependência. `exporta_rtmpose.py` chama este script; para consertar um arquivo
já exportado:

    ~/.cache/lince/rtmpose-export/venv/bin/python apps/agent/tools/fixa_formas_rtmpose.py \\
        ~/lince-modelos/rtmpose-s.onnx ~/lince-modelos/rtmpose-s.onnx
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

NUM_JUNTAS = 17
ENTRADA = (3, 256, 192)
"""Canal, altura e largura do recorte do RTMPose-s 256×192."""
BINS = {384: "x", 512: "y"}
"""Largura e altura do recorte vezes o `simcc_split_ratio` 2.0."""
LOTE_DE_OBSERVACAO = 2
"""Dois, não um: um grafo com o lote preso em 1 por dentro aparece aqui."""
NOME_DO_LOTE = "batch"


class FormaError(Exception):
    """O grafo não devolve o SimCC do RTMPose-s COCO-17."""


def observa(caminho: Path) -> dict[str, tuple[int, ...]]:
    import numpy as np
    import onnxruntime as ort

    sessao = ort.InferenceSession(str(caminho), providers=["CPUExecutionProvider"])
    entradas = sessao.get_inputs()
    if len(entradas) != 1:
        raise FormaError(f"esperava uma entrada, o arquivo tem {len(entradas)}")
    tensor = np.zeros((LOTE_DE_OBSERVACAO, *ENTRADA), dtype=np.float32)
    nomes = [saida.name for saida in sessao.get_outputs()]
    resultados = sessao.run(nomes, {entradas[0].name: tensor})
    return {nome: tuple(int(v) for v in r.shape) for nome, r in zip(nomes, resultados, strict=True)}


def confere(observadas: dict[str, tuple[int, ...]]) -> dict[str, int]:
    """Os bins de cada saída, se o grafo é mesmo o SimCC COCO-17."""
    if len(observadas) != 2:
        raise FormaError(f"o SimCC tem duas saídas, o grafo devolveu {list(observadas)}")
    bins_por_saida: dict[str, int] = {}
    for nome, forma in observadas.items():
        if len(forma) != 3 or forma[0] != LOTE_DE_OBSERVACAO or forma[1] != NUM_JUNTAS:
            raise FormaError(
                f"{nome} devolveu {forma} para um lote de {LOTE_DE_OBSERVACAO}; "
                f"o RTMPose-s COCO-17 devolve ({LOTE_DE_OBSERVACAO}, {NUM_JUNTAS}, bins)"
            )
        if forma[2] not in BINS:
            raise FormaError(
                f"{nome} tem {forma[2]} bins; o recorte 256×192 com split ratio 2.0 "
                f"dá {sorted(BINS)}"
            )
        bins_por_saida[nome] = forma[2]
    if sorted(bins_por_saida.values()) != sorted(BINS):
        raise FormaError(f"as duas saídas têm o mesmo eixo: {bins_por_saida}")
    return bins_por_saida


def _grava_dims(valor_info: object, dims: list[int | str]) -> None:
    forma = valor_info.type.tensor_type.shape  # type: ignore[attr-defined]
    del forma.dim[:]
    for dim in dims:
        novo = forma.dim.add()
        if isinstance(dim, str):
            novo.dim_param = dim
        else:
            novo.dim_value = dim


def fixa(entrada: Path, saida: Path) -> dict[str, int]:
    import onnx

    bins = confere(observa(entrada))
    modelo = onnx.load(str(entrada))
    _grava_dims(modelo.graph.input[0], [NOME_DO_LOTE, *ENTRADA])
    for valor_info in modelo.graph.output:
        _grava_dims(valor_info, [NOME_DO_LOTE, NUM_JUNTAS, bins[valor_info.name]])
    onnx.checker.check_model(modelo)
    # Para um temporário ao lado: interromper no meio não pode deixar um
    # destino truncado, e a entrada pode ser o próprio destino.
    parcial = saida.with_name(saida.name + ".parcial")
    onnx.save(modelo, str(parcial))
    os.replace(parcial, saida)
    return bins


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("uso: fixa_formas_rtmpose.py ENTRADA.onnx SAIDA.onnx", file=sys.stderr)
        return 2
    entrada, saida = Path(args[0]).expanduser(), Path(args[1]).expanduser()
    try:
        bins = fixa(entrada, saida)
    except FormaError as erro:
        print(f"fixa_formas_rtmpose: {erro}. Nada foi gravado.", file=sys.stderr)
        return 1
    print(
        f"formas gravadas em {saida}: entrada ({NOME_DO_LOTE}, 3, 256, 192), "
        + ", ".join(f"{nome} ({NOME_DO_LOTE}, {NUM_JUNTAS}, {b})" for nome, b in bins.items()),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
