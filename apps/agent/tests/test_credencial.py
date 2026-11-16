"""A credencial do agente no disco do box (§5.1).

Ela é entregue uma vez e não existe em mais lugar nenhum: a nuvem guarda o hash, e o
token de bootstrap que a comprou já foi gasto. Tudo aqui gira em torno de não perdê-la
— e de não deixá-la legível para quem não devia.
"""

from __future__ import annotations

import json
import logging
import stat

import pytest

from lince_agent.credencial import CofreDeCredencial, Credencial

SEGREDO = "tok-" + "a1b2" * 16


def credencial(**sobrescreve) -> Credencial:
    campos = {"agent_id": "ag-1", "tenant_id": "rede-1", "store_id": "loja-7", "token": SEGREDO}
    return Credencial(**{**campos, **sobrescreve})


def test_o_que_se_grava_e_o_que_se_le_de_volta(tmp_path):
    """O reinício do box depende só disto: sem a ida e volta exata, toda atualização de
    imagem pediria um token de bootstrap novo e uma visita ao dashboard."""
    cofre = CofreDeCredencial(tmp_path / "estado" / "credencial.json")

    assert cofre.grava(credencial()) is True

    assert CofreDeCredencial(cofre.caminho).le() == credencial()


def test_o_arquivo_nasce_legivel_so_pelo_dono(tmp_path):
    """O token abre a configuração da loja, com as senhas RTSP dentro. `0644` o
    entregaria a qualquer processo do host que leia o volume."""
    cofre = CofreDeCredencial(tmp_path / "credencial.json")

    cofre.grava(credencial())

    assert stat.S_IMODE(cofre.caminho.stat().st_mode) == 0o600


def test_regravar_por_cima_de_um_temporario_frouxo_ainda_sai_0600(tmp_path):
    """Um `.novo` que sobrou de uma queda de energia pode ter qualquer modo. Se a
    gravação o reaproveitasse, a credencial herdaria a permissão do resto."""
    cofre = CofreDeCredencial(tmp_path / "credencial.json")
    sobra = tmp_path / "credencial.json.novo"
    sobra.write_text("lixo")
    sobra.chmod(0o644)

    assert cofre.grava(credencial()) is True

    assert stat.S_IMODE(cofre.caminho.stat().st_mode) == 0o600
    assert not sobra.exists()


def test_gravacao_que_falha_nao_destroi_a_credencial_anterior(tmp_path):
    """A escrita é atômica: ou a antiga inteira, ou a nova inteira. Aqui o `replace`
    falha porque o destino é um diretório — por estrutura, não por permissão, que o
    root do container ignoraria (regra 7)."""
    destino = tmp_path / "credencial.json"
    destino.mkdir()
    (destino / "ocupado").write_text("x")
    cofre = CofreDeCredencial(destino)

    assert cofre.grava(credencial()) is False

    assert (destino / "ocupado").exists()
    assert not (tmp_path / "credencial.json.novo").exists()


def test_o_token_nao_aparece_no_repr_nem_no_log_de_leitura(tmp_path, caplog):
    """`Credencial` é o tipo que mais fácil cai num `log.info("%s", ...)`. E o log do
    box sai da loja pelo `docker compose logs` de quem der suporte."""
    cofre = CofreDeCredencial(tmp_path / "credencial.json")
    cofre.grava(credencial())

    with caplog.at_level(logging.DEBUG):
        lida = cofre.le()

    assert SEGREDO not in repr(lida)
    assert SEGREDO not in str(lida)
    assert SEGREDO not in caplog.text


def test_sem_arquivo_nao_ha_credencial_e_nao_ha_alarme(tmp_path, caplog):
    """É a primeira subida de todo box. Um erro no log aqui seria ruído em toda
    instalação, e ensinaria a ignorar o erro de verdade."""
    with caplog.at_level(logging.WARNING):
        assert CofreDeCredencial(tmp_path / "credencial.json").le() is None

    assert caplog.text == ""


@pytest.mark.parametrize(
    "conteudo",
    [
        '{"formato": 1, "agent_id": "ag-1", "tenant_id": "rede-1", "store_id"',
        json.dumps(
            {"formato": 99, "agent_id": "a", "tenant_id": "t", "store_id": "s", "token": "x"}
        ),
        json.dumps({"formato": 1, "agent_id": "a", "tenant_id": "t", "store_id": "s"}),
        json.dumps({"formato": 1, "agent_id": "a", "tenant_id": "t", "store_id": "s", "token": ""}),
        json.dumps(["nao", "e", "objeto"]),
    ],
    ids=["truncado", "formato-desconhecido", "sem-token", "token-vazio", "nao-objeto"],
)
def test_arquivo_estragado_vira_ausencia_com_erro_no_log(tmp_path, caplog, conteudo):
    """Nunca levanta: quem chama precisa chegar à mensagem que diz o que fazer (gerar
    outro token), e um traceback de JSON a enterraria. Mas avisa — aqui a ausência
    custa uma ida ao dashboard, e alguém vai querer saber por quê."""
    caminho = tmp_path / "credencial.json"
    caminho.write_text(conteudo)

    with caplog.at_level(logging.ERROR):
        assert CofreDeCredencial(caminho).le() is None

    assert str(caminho) in caplog.text


def test_o_log_de_arquivo_corrompido_nao_cita_o_conteudo(tmp_path, caplog):
    """O `JSONDecodeError` cita o trecho que não conseguiu ler — e o trecho de um
    arquivo de credencial truncado é a credencial."""
    caminho = tmp_path / "credencial.json"
    caminho.write_text(f'{{"formato": 1, "token": "{SEGREDO}", ')

    with caplog.at_level(logging.DEBUG):
        CofreDeCredencial(caminho).le()

    assert SEGREDO not in caplog.text


def test_pode_gravar_diz_sim_e_nao_deixa_rastro(tmp_path):
    cofre = CofreDeCredencial(tmp_path / "estado" / "credencial.json")

    assert cofre.pode_gravar() is True

    assert list((tmp_path / "estado").iterdir()) == []


def test_pode_gravar_diz_nao_antes_de_alguem_gastar_o_token(tmp_path):
    """É a pergunta que se faz ANTES do registro: descobrir que o volume não aceita
    escrita depois de trocar o token é perder a credencial. O pai é um arquivo, o que
    falha para qualquer usuário, inclusive root (regra 7)."""
    pai = tmp_path / "nao-e-diretorio"
    pai.write_text("x")
    cofre = CofreDeCredencial(pai / "credencial.json")

    assert cofre.pode_gravar() is False
    assert cofre.grava(credencial()) is False
