"""Do documento da §5.2 para o `AgentConfig`.

Este loader é a única porta por onde as zonas entram no agente — hoje vindas de um
arquivo, amanhã do `GET /v1/agents/config`. O que se testa aqui não é conversão de
tipo: é o que acontece com a loja quando o documento está errado. Zona que o loader
aceita e o motor de regras não entende vira câmera alertando para todo cliente que
sai (R-1), e o único sintoma é o gerente desligando a notificação.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lince_agent.config import DecodeOptions, HwAccel, PixelFormat
from lince_agent.config_loader import ConfigError, carrega_arquivo, le_documento, monta_config
from lince_agent.rules.geometry import LinhaOrientada, Poligono

EXEMPLO = Path(__file__).resolve().parents[1] / "config.exemplo.json"


def documento(**mudancas) -> dict:
    """Documento mínimo válido: uma câmera, sem regras e sem detecção."""
    base = {
        "schema_version": 1,
        "config_version": "v1",
        "tenant_id": "rede-abc",
        "store_id": "loja-1",
        "cameras": [{"camera_id": "cam1", "url": "rtsp://camera/stream"}],
    }
    return base | mudancas


def monta(doc: dict, tmp_path: Path, **kwargs):
    return monta_config(doc, clips_dir=tmp_path / "clipes", **kwargs)


def com_regras(**mudancas) -> dict:
    """Uma câmera com a regra do §3.4 ligada, dentro de um quadro de 640x480."""
    regras = {
        "enabled": True,
        "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
        "zonas_caixa": [{"vertices": [[0, 410], [260, 410], [260, 478], [0, 478]]}],
    } | mudancas
    return documento(cameras=[{"camera_id": "cam3", "url": "rtsp://x/y", "rules": regras}])


def test_exemplo_do_repositorio_carrega(tmp_path):
    """`config.exemplo.json` é o que alguém copia para subir a primeira loja. Se ele
    parar de carregar, a documentação passa a ensinar um formato que não existe."""
    config = carrega_arquivo(EXEMPLO, clips_dir=tmp_path, model_path=tmp_path / "yolox_s.onnx")

    assert config.config_version == "exemplo-1"
    assert [camera.camera_id for camera in config.cameras] == ["cam3", "cam1"]

    cam3 = config.cameras[0]
    assert cam3.rules.enabled
    assert cam3.rules.linha_saida == LinhaOrientada((0.0, 400.0), (640.0, 400.0))
    assert cam3.rules.zonas_caixa[0].contem((100.0, 450.0))
    # A segunda câmera não tem zonas e continua válida: é o caso R-3, câmera que grava
    # clipe e responde a gatilho manual sem decidir nada.
    assert not config.cameras[1].rules.enabled


def test_bloco_ausente_fica_com_o_default_do_agente(tmp_path):
    """Documento não repete default. Se ele passasse a repetir, mudar 3 fps no
    `config.py` deixaria de valer — e o dimensionamento inteiro do §3.1 mora ali."""
    config = monta(documento(), tmp_path)

    assert config.cameras[0].decode == DecodeOptions()
    assert config.cameras[0].detect is True


def test_zonas_viram_geometria_de_verdade(tmp_path):
    """A linha precisa chegar orientada e o polígono fechado, senão o §3.4 decide sobre
    outra coisa que não a porta da loja."""
    config = monta(com_regras(), tmp_path)
    regras = config.cameras[0].rules

    assert isinstance(regras.linha_saida, LinhaOrientada)
    assert isinstance(regras.zonas_caixa[0], Poligono)
    # Orientação: dentro da loja é embaixo (y maior), como manda a convenção do §3.4.
    assert regras.linha_saida.dentro((320.0, 450.0))
    assert not regras.linha_saida.dentro((320.0, 100.0))


def test_linha_nula_e_ausencia_de_linha_sao_a_mesma_coisa(tmp_path):
    """A nuvem precisa poder apagar uma linha sem omitir a chave: `null` explícito é
    "esta câmera não tem mais zona", e recusá-lo como erro de tipo deixaria a loja
    presa na calibração antiga."""
    doc = documento(cameras=[{"camera_id": "c", "url": "u", "rules": {"linha_saida": None}}])
    config = monta(doc, tmp_path)

    assert config.cameras[0].rules.linha_saida is None


def test_campo_com_nome_errado_e_recusado_com_o_nome_certo(tmp_path):
    """O caso que motiva `additionalProperties: false` nos dois lados.

    `zona_caixa` no singular é um erro de digitação plausível — e engoli-lo em
    silêncio faz a câmera subir com a regra ligada e nenhuma zona de caixa: todo mundo
    tem zero segundo no caixa, e toda saída da loja vira alerta (R-1). Quem digitou
    acha que calibrou.
    """
    doc = com_regras()
    doc["cameras"][0]["rules"]["zona_caixa"] = doc["cameras"][0]["rules"].pop("zonas_caixa")

    with pytest.raises(ConfigError) as erro:
        monta(doc, tmp_path)

    assert "zona_caixa" in str(erro.value)
    assert "zonas_caixa" in str(erro.value), "a mensagem tem que mostrar o nome certo"
    assert "cameras[0].rules" in str(erro.value)


def test_erro_diz_em_qual_camera_esta(tmp_path):
    """Numa loja de oito câmeras, "esperava número" sem o caminho manda conferir as
    oito. O índice é o que transforma o erro em conserto."""
    doc = documento(
        cameras=[
            {"camera_id": "cam1", "url": "rtsp://a"},
            {"camera_id": "cam2", "url": "rtsp://b", "decode": {"sample_fps": "três"}},
        ]
    )

    with pytest.raises(ConfigError, match=r"cameras\[1\]\.decode\.sample_fps"):
        monta(doc, tmp_path)


def test_booleano_no_lugar_de_numero_e_recusado(tmp_path):
    """`isinstance(True, int)` é verdadeiro em Python: sem guarda, `"sample_fps": true`
    viraria 1 fps sem uma linha de log. A 1 fps o tracker perde quem anda — e quem sai
    da loja anda."""
    doc = documento(cameras=[{"camera_id": "c", "url": "u", "decode": {"sample_fps": True}}])

    with pytest.raises(ConfigError, match="esperava número"):
        monta(doc, tmp_path)


def test_valor_de_enum_invalido_lista_os_aceitos(tmp_path):
    doc = documento(cameras=[{"camera_id": "c", "url": "u", "decode": {"pixel_format": "rgb24"}}])

    with pytest.raises(ConfigError) as erro:
        monta(doc, tmp_path)

    assert "rgb24" in str(erro.value)
    for aceito in (PixelFormat.BGR24, PixelFormat.NV12, PixelFormat.YUV420P):
        assert aceito.value in str(erro.value)


def test_hwaccel_do_documento_e_pedido_e_chega_como_pedido(tmp_path):
    """O loader não sonda a máquina: o fallback para CPU é do §3.2 e acontece depois,
    em tempo de execução. Sondar aqui tornaria carregar configuração uma operação que
    depende de ffmpeg instalado — e o mesmo documento deixaria de produzir o mesmo
    objeto em duas máquinas."""
    doc = documento(cameras=[{"camera_id": "c", "url": "u", "decode": {"hwaccel": "cuda"}}])

    assert monta(doc, tmp_path).cameras[0].decode.hwaccel is HwAccel.CUDA


def test_major_diferente_nao_e_aplicada_pela_metade(tmp_path):
    """Uma major nova muda campo obrigatório. Aplicar o que dá e ignorar o resto
    deixaria a loja rodando com metade da calibração e nenhum aviso — pior que o
    agente não subir, porque ele continuaria mandando eventos."""
    with pytest.raises(ConfigError, match="schema_version"):
        monta(documento(schema_version=2), tmp_path)


def test_falta_de_campo_obrigatorio_diz_qual(tmp_path):
    doc = documento()
    del doc["store_id"]

    with pytest.raises(ConfigError, match="store_id"):
        monta(doc, tmp_path)


def test_camera_sem_url_e_recusada(tmp_path):
    with pytest.raises(ConfigError, match=r"cameras\[0\].*url"):
        monta(documento(cameras=[{"camera_id": "cam1"}]), tmp_path)


def test_zona_fora_do_quadro_e_recusada_na_subida(tmp_path):
    """Zona desenhada sobre um frame de outra resolução não contém ninguém: tempo de
    caixa zero para todo mundo, e a câmera alerta a loja inteira (R-1). É o caminho
    mais curto do sistema até o falso positivo em massa, e o único sintoma seria o
    volume de alertas."""
    doc = com_regras(zonas_caixa=[{"vertices": [[0, 900], [1200, 900], [1200, 1000]]}])

    with pytest.raises(ConfigError) as erro:
        monta(doc, tmp_path)

    assert "cameras[0]" in str(erro.value)
    assert "640x480" in str(erro.value)


def test_regra_ligada_sem_zona_de_caixa_e_recusada(tmp_path):
    with pytest.raises(ConfigError, match="zona de caixa"):
        monta(com_regras(zonas_caixa=[]), tmp_path)


def test_poligono_de_dois_vertices_e_recusado(tmp_path):
    """Dois vértices têm área zero: a zona nunca conteria ninguém, e o efeito é o
    mesmo da zona fora do quadro."""
    with pytest.raises(ConfigError, match=r"cameras\[0\]\.rules\.zonas_caixa\[0\]"):
        monta(com_regras(zonas_caixa=[{"vertices": [[0, 0], [1, 1]]}]), tmp_path)


def test_ponto_com_tres_numeros_e_recusado(tmp_path):
    """`[x, y, z]` costuma ser um vértice colado de outro formato. Aceitá-lo lendo só
    os dois primeiros números produziria uma zona plausível e deslocada."""
    doc = com_regras(linha_saida={"origem": [0, 400, 0], "destino": [640, 400]})

    with pytest.raises(ConfigError, match="linha_saida.origem"):
        monta(doc, tmp_path)


def test_camera_id_repetido_e_recusado(tmp_path):
    """Dois ids iguais mandam o clipe para o buffer da câmera errada — o triador
    recebe o vídeo de outro corredor e descarta o alerta como engano."""
    doc = documento(
        cameras=[
            {"camera_id": "cam1", "url": "rtsp://a"},
            {"camera_id": "cam1", "url": "rtsp://b"},
        ]
    )

    with pytest.raises(ConfigError, match="cam1"):
        monta(doc, tmp_path)


def test_deteccao_ligada_sem_modelo_no_box_explica_de_quem_e_a_metade_que_falta(tmp_path):
    """O caminho do `.onnx` é do box, não do documento. Uma mensagem dizendo só "exige
    model_path" mandaria procurar o campo no JSON, onde ele não existe nem deve
    existir: o que desce da nuvem é o binário com checksum (§5.2)."""
    with pytest.raises(ConfigError) as erro:
        monta(documento(detection={"enabled": True}), tmp_path)

    assert "--model" in str(erro.value)


def test_deteccao_combina_limiar_da_nuvem_com_caminho_do_box(tmp_path):
    """A divisão que o §5.2 exige: a nuvem manda **quais** limiares, o box sabe
    **onde** está o arquivo."""
    config = monta(
        documento(detection={"enabled": True, "score_threshold": 0.2}),
        tmp_path,
        model_path=Path("/opt/lince/yolox_s.onnx"),
        providers=("CPUExecutionProvider",),
    )

    assert config.detection.enabled
    assert config.detection.score_threshold == 0.2
    assert config.detection.model_path == Path("/opt/lince/yolox_s.onnx")
    assert config.detection.providers == ("CPUExecutionProvider",)


def test_config_version_chega_ao_agente(tmp_path):
    """Ela viaja no evento (`versions.config`). Sem ela, comparar a taxa de falso
    positivo de duas semanas não significa nada: metade dos eventos nasceu sob outros
    limiares (§6)."""
    assert monta(documento(config_version="cfg-42"), tmp_path).config_version == "cfg-42"


def test_config_version_longa_demais_e_recusada_na_subida(tmp_path):
    """O contrato aceita 64 caracteres. Descobrir isso em produção significaria a loja
    inteira indo para a fila morta por `4xx` — e só quando o link voltasse."""
    with pytest.raises(ConfigError, match="64"):
        monta(documento(config_version="x" * 65), tmp_path)


def test_arquivo_que_nao_e_json_diz_qual_arquivo(tmp_path):
    """Quem sobe um agente costuma ter dois arquivos abertos, o da loja e o de
    exemplo. `line 42 column 7` sem o nome do arquivo manda depurar o errado."""
    caminho = tmp_path / "loja.json"
    caminho.write_text("{ isto não é json }", encoding="utf-8")

    with pytest.raises(ConfigError, match="loja.json"):
        le_documento(caminho)


def test_arquivo_inexistente_e_erro_de_configuracao(tmp_path):
    with pytest.raises(ConfigError, match="não consegui ler"):
        carrega_arquivo(tmp_path / "nao-existe.json", clips_dir=tmp_path)


def test_raiz_que_nao_e_objeto_e_recusada(tmp_path):
    """Uma lista de câmeras na raiz é o formato que alguém inventaria de cabeça."""
    with pytest.raises(ConfigError, match="objeto na raiz"):
        monta_config([{"camera_id": "cam1"}], clips_dir=tmp_path)


def test_documento_carregado_do_disco_e_o_mesmo_de_um_dict(tmp_path):
    """`le_documento` não pode transformar nada: quando o `GET /v1/agents/config`
    existir, o dicionário virá de `json.loads` do corpo da resposta, e os dois caminhos
    precisam produzir o mesmo `AgentConfig`."""
    doc = com_regras()
    caminho = tmp_path / "loja.json"
    caminho.write_text(json.dumps(doc), encoding="utf-8")

    do_disco = carrega_arquivo(caminho, clips_dir=tmp_path)
    da_memoria = monta_config(doc, clips_dir=tmp_path)

    assert do_disco == da_memoria
