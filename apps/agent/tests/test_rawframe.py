"""Leitura do pipe de detecção, contra pipes de verdade."""

from __future__ import annotations

import os
import threading

import numpy as np
import pytest

from lince_agent.config import PixelFormat
from lince_agent.ffmpeg.rawframe import Frame, iter_raw_frames, read_exact


def pipe_com(dados: bytes, *, pedacos: int = 1) -> int:
    """Escreve `dados` num pipe a partir de outra thread e devolve a ponta de
    leitura. A escrita precisa ser concorrente: mais que 64 KB não cabem no buffer
    do kernel e o escritor bloquearia até alguém ler."""
    leitura, escrita = os.pipe()

    def escreve() -> None:
        passo = max(1, len(dados) // pedacos)
        try:
            for offset in range(0, len(dados), passo):
                os.write(escrita, dados[offset : offset + passo])
        finally:
            os.close(escrita)

    threading.Thread(target=escreve, daemon=True).start()
    return leitura


def test_le_exatamente_o_pedido():
    fd = pipe_com(b"abcdefghij")
    try:
        assert read_exact(fd, 4) == b"abcd"
        assert read_exact(fd, 6) == b"efghij"
    finally:
        os.close(fd)


def test_remonta_frame_partido_em_muitos_pedacos():
    """Um frame de 921.600 bytes chega em ~14 leituras de 64 KB: o pipe entrega o
    que tiver, não o que foi pedido."""
    dados = bytes(range(256)) * 4000
    fd = pipe_com(dados, pedacos=50)
    try:
        assert read_exact(fd, len(dados)) == dados
    finally:
        os.close(fd)


def test_eof_devolve_none():
    fd = pipe_com(b"")
    try:
        assert read_exact(fd, 10) is None
    finally:
        os.close(fd)


def test_frame_parcial_no_eof_e_descartado():
    """Meio frame não é frame: entregá-lo produziria uma imagem cortada com lixo no
    resto, que o detector processaria sem reclamar."""
    fd = pipe_com(b"abc")
    try:
        assert read_exact(fd, 10) is None
    finally:
        os.close(fd)


def test_itera_frames_ate_o_eof():
    fd = pipe_com(b"AAAABBBBCCCC")
    try:
        assert list(iter_raw_frames(fd, 4)) == [b"AAAA", b"BBBB", b"CCCC"]
    finally:
        os.close(fd)


def test_sobra_incompleta_no_fim_nao_vira_frame():
    fd = pipe_com(b"AAAABB")
    try:
        assert list(iter_raw_frames(fd, 4)) == [b"AAAA"]
    finally:
        os.close(fd)


def frame(pixel_format: PixelFormat, width: int = 4, height: int = 2) -> Frame:
    return Frame(
        data=bytes(pixel_format.frame_bytes(width, height)),
        width=width,
        height=height,
        pixel_format=pixel_format,
        sequence=1,
        received_at=0.0,
    )


def test_bgr24_vira_array_no_formato_do_detector():
    array = frame(PixelFormat.BGR24).as_array()
    assert array.shape == (2, 4, 3)
    assert array.dtype == np.uint8


@pytest.mark.parametrize("pixel_format", [PixelFormat.NV12, PixelFormat.YUV420P])
def test_planares_saem_no_layout_que_o_cvtcolor_espera(pixel_format: PixelFormat):
    """4:2:0 é entregue como um plano só, de altura 3/2 — é essa a forma que o
    `cv2.cvtColor` recebe."""
    assert frame(pixel_format).as_array().shape == (3, 4)


def test_array_nao_copia_os_bytes():
    """Copiar 921.600 bytes por frame, 3 vezes por segundo, em 8 câmeras, seria
    22 MB/s de cópia inútil."""
    original = frame(PixelFormat.BGR24)
    assert original.as_array().base is not None
