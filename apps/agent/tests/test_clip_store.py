"""O diretório de clipes pendentes e o teto de disco (§3.6).

Enquanto o estágio 6 não existe, isto é a única coisa entre o box da loja e um
disco cheio. Disco cheio no box não degrada só o clipe: derruba o log, o SQLite da
fila e, com eles, a detecção inteira.
"""

from __future__ import annotations

import os

import pytest

from lince_agent.clip.store import ClipStore


def escreve(store: ClipStore, event_id: str, tamanho: int, *, mtime: float | None = None):
    caminho = store.path_for(event_id)
    caminho.write_bytes(b"\0" * tamanho)
    if mtime is not None:
        os.utime(caminho, (mtime, mtime))
    store.register(caminho)
    return caminho


def test_clipe_nasce_no_diretorio_configurado(tmp_path):
    store = ClipStore(tmp_path / "pendentes")

    caminho = store.path_for("11111111-2222-3333-4444-555555555555")

    assert caminho.parent == tmp_path / "pendentes"
    assert caminho.name.endswith(".mp4")
    assert caminho.parent.is_dir(), "o diretório é criado na subida, não no primeiro clipe"


def test_event_id_nao_escapa_do_diretorio(tmp_path):
    """O `event_id` é um UUID gerado na borda (§3.6), mas ele vira **nome de
    arquivo**. Um id com `..` ou `/` escreveria fora do diretório de clipes, e o
    lugar barato de barrar isso é aqui, não em cada caminho que produz um id."""
    store = ClipStore(tmp_path)

    for maligno in ("../fuga", "a/b", "/etc/passwd", "", "."):
        with pytest.raises(ValueError, match="event_id"):
            store.path_for(maligno)


def test_teto_de_disco_apaga_o_clipe_mais_antigo(tmp_path):
    """§3.6, literal: "ao atingir o teto, descarta-se o clipe mais antigo (o evento
    sobrevive sem clipe)". Alertar sobre um furto de agora vale mais que guardar o
    vídeo de um de ontem."""
    store = ClipStore(tmp_path, max_bytes=300)
    escreve(store, "antigo", 100, mtime=1000)
    escreve(store, "medio", 100, mtime=2000)

    escreve(store, "novo", 200, mtime=3000)

    assert not store.path_for("antigo").exists()
    assert store.path_for("novo").exists()
    assert store.usage_bytes() <= 300


def test_teto_nunca_apaga_o_unico_clipe(tmp_path):
    """Um clipe sozinho maior que o teto significa teto mal dimensionado. Apagá-lo
    deixaria o evento sem vídeo sem resolver o problema."""
    store = ClipStore(tmp_path, max_bytes=10)

    escreve(store, "gordo", 5000)

    assert store.path_for("gordo").exists()


def test_descarte_apos_upload_remove_o_arquivo(tmp_path):
    """NFR-3: o clipe é o único vídeo em disco e não fica. É o estágio 6 que chama
    isto depois de o upload ser confirmado."""
    store = ClipStore(tmp_path)
    escreve(store, "enviado", 50)

    assert store.discard("enviado") is True
    assert not store.path_for("enviado").exists()
    assert store.usage_bytes() == 0


def test_descarte_do_que_nao_existe_nao_estoura(tmp_path):
    """Retry de upload que chega duas vezes não pode derrubar a thread do envio."""
    store = ClipStore(tmp_path)

    assert store.discard("nunca-existiu") is False


def test_registrar_arquivo_ausente_e_erro(tmp_path):
    """Se o remux disse que escreveu e não escreveu, isso precisa aparecer agora —
    e não como um evento apontando para um arquivo que não existe."""
    store = ClipStore(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.register(store.path_for("fantasma"))


def test_usage_bytes_conta_so_os_clipes(tmp_path):
    """O diretório pode ter parcial de outro processo ou lixo do sistema; o teto se
    aplica ao que é clipe."""
    store = ClipStore(tmp_path)
    escreve(store, "um", 100)
    (tmp_path / "anotacao.txt").write_bytes(b"\0" * 999)

    assert store.usage_bytes() == 100


def test_teto_precisa_ser_positivo(tmp_path):
    with pytest.raises(ValueError, match="max_bytes"):
        ClipStore(tmp_path, max_bytes=0)
