"""O cache local da última configuração válida (§5.4).

O cenário que este módulo existe para resolver não é o poll em regime — é a **subida**
com o link caído. Um box reinicia por queda de energia, watchtower ou `docker restart`,
e a internet da loja está fora. Sem cache, o agente não tem câmera nem zona e não sobe:
a loja fica cega exatamente no dia em que ninguém consegue chegar nela remotamente.

Por isso os testes daqui são quase todos sobre **falhar bem**: cache truncado, cache de
formato desconhecido, disco que não aceita escrita. Nenhum deles pode derrubar o
agente, porque em todos eles o agente ainda tem um caminho — ir à nuvem.
"""

from __future__ import annotations

from lince_agent.config_cache import FORMATO, ConfigCache
from lince_agent.config_loader import monta_config

DOCUMENTO = {
    "schema_version": 1,
    "config_version": "v7",
    "tenant_id": "rede-abc",
    "store_id": "loja-1",
    "cameras": [
        {
            "camera_id": "cam3",
            "url": "rtsp://x/cam3",
            "rules": {
                "enabled": True,
                "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
                "zonas_caixa": [{"vertices": [[0, 410], [260, 410], [260, 478], [0, 478]]}],
            },
        }
    ],
}


def test_ida_e_volta_passa_pelo_loader(tmp_path):
    """O que volta do cache tem que montar um `AgentConfig` de verdade, com as zonas
    inteiras. Guardar o documento cru só vale se ele continuar sendo o mesmo formato que
    a nuvem serve — que é a razão de o cache não serializar o `AgentConfig`."""
    cache = ConfigCache(tmp_path / "config-cache.json")
    cache.grava(DOCUMENTO, etag='"v7"')

    guardado = cache.le()

    assert guardado is not None
    assert guardado.etag == '"v7"'
    config = monta_config(guardado.documento, clips_dir=tmp_path / "clipes")
    assert config.config_version == "v7"
    assert config.cameras[0].rules.zonas_caixa[0].contem((100.0, 450.0))


def test_sem_arquivo_e_none_e_nao_erro(tmp_path):
    """Primeira subida de um box novo. Levantar aqui faria a ausência esperada de cache
    virar falha de inicialização."""
    assert ConfigCache(tmp_path / "nao-existe.json").le() is None


def test_cache_truncado_e_ignorado_em_vez_de_derrubar(tmp_path):
    """Queda de energia no meio de uma gravação não atômica deixaria isto no disco. Se
    um JSON pela metade derrubasse a subida, o cache — que existe justamente para o
    reinício sem rede — passaria a ser o que impede o reinício."""
    caminho = tmp_path / "config-cache.json"
    caminho.write_text('{"formato": 1, "documento": {"cam', encoding="utf-8")

    assert ConfigCache(caminho).le() is None


def test_formato_desconhecido_e_ignorado(tmp_path):
    """Cache escrito por uma versão futura do agente, depois de um rollback de imagem
    (ADR-006). Interpretá-lo com as regras antigas é pior que não ter cache: dá uma
    configuração plausível e errada, e o agente sempre pode buscar da nuvem."""
    caminho = tmp_path / "config-cache.json"
    caminho.write_text(f'{{"formato": {FORMATO + 1}, "documento": {{}}}}', encoding="utf-8")

    assert ConfigCache(caminho).le() is None


def test_envelope_sem_documento_e_ignorado(tmp_path):
    """Um envelope com o formato certo e sem conteúdo passaria pela primeira guarda e
    entregaria `{}` ao loader — que recusaria com uma mensagem sobre campo obrigatório
    ausente, mandando procurar o erro no documento da nuvem em vez de no cache."""
    caminho = tmp_path / "config-cache.json"
    caminho.write_text(f'{{"formato": {FORMATO}}}', encoding="utf-8")

    assert ConfigCache(caminho).le() is None


def test_gravacao_e_atomica(tmp_path):
    """`os.replace` sobre um temporário: ou o leitor vê o cache antigo inteiro, ou o
    novo inteiro. Escrever por cima do arquivo final deixaria uma janela em que ele
    existe truncado — e numa queda de energia essa janela vira o estado permanente."""
    caminho = tmp_path / "config-cache.json"
    cache = ConfigCache(caminho)
    cache.grava(DOCUMENTO, etag='"v7"')
    cache.grava(DOCUMENTO | {"config_version": "v8"}, etag='"v8"')

    guardado = cache.le()

    assert guardado is not None
    assert guardado.documento["config_version"] == "v8"
    # Nenhum temporário sobrevive à gravação: um `.novo` órfão no disco do box seria
    # confundido com o cache de verdade por quem for depurar uma loja.
    assert [p.name for p in tmp_path.iterdir()] == ["config-cache.json"]


def test_cria_o_diretorio_se_ele_nao_existe(tmp_path):
    """O default do cache fica ao lado dos clipes, e o diretório de clipes pode ainda
    não ter sido criado no primeiro boot do container."""
    cache = ConfigCache(tmp_path / "fundo" / "config-cache.json")

    assert cache.grava(DOCUMENTO) is True
    assert cache.le() is not None


def test_falha_de_escrita_nao_levanta(tmp_path):
    """Disco cheio, volume só-leitura, caminho de cache impossível. O agente acabou de
    aplicar uma configuração válida e está detectando; não conseguir guardá-la para a
    próxima subida é um problema real, mas parar o pipeline por causa dele violaria a
    §5.4.

    A falha é provocada por um caminho cujo pai é um arquivo comum, e não por
    `chmod(0o500)` num diretório: o agente roda como root dentro do container (§3.8), e
    o uid 0 ignora bit de permissão. Um teste construído sobre permissão passa na
    máquina do dev e falha em CI e no box — que são justamente os dois lugares onde ele
    precisa valer. O que se está testando é o contrato de `grava` diante de um `OSError`
    qualquer: `False` com log, nunca exceção."""
    ocupado = tmp_path / "isto-e-um-arquivo"
    ocupado.write_text("não sou diretório", encoding="utf-8")

    assert ConfigCache(ocupado / "config-cache.json").grava(DOCUMENTO) is False
