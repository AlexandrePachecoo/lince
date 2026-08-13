"""O payload que o agente produz contra o schema de `packages/shared` (§5).

Este é o teste que impede a duplicação mais cara do projeto: a definição do contrato
existir em dois lugares e divergir. O schema é a fonte; o agente é um consumidor dela
como a API será. Se alguém acrescentar um campo aqui e esquecer lá, o teste quebra
antes de o evento ser recusado em produção — que é onde isso apareceria, numa loja,
como `4xx` de validação e fila morta crescendo.

Nada de rede: os `$ref` entre schemas são resolvidos por um registry montado a partir
dos arquivos do repositório.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eventos import IDENTIDADE, INSTANTE, rascunho, resultado
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from lince_agent.clip.state import ClipStatus
from lince_agent.outbox.event import (
    EventSource,
    build_clip_patch,
    build_event_payload,
    clip_block,
    new_event_id,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "packages" / "shared" / "schemas"


@pytest.fixture(scope="session")
def registry() -> Registry:
    recursos = []
    for arquivo in sorted(SCHEMAS.glob("*.json")):
        conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
        recursos.append((conteudo["$id"], Resource.from_contents(conteudo)))
    assert recursos, f"nenhum schema em {SCHEMAS}"
    return Registry().with_resources(recursos)


def validador(nome: str, registry: Registry) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / nome).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, registry=registry)


@pytest.mark.parametrize("nome", ["common.v1.json", "event.v1.json", "event-clip.v1.json"])
def test_o_schema_em_si_e_valido(nome, registry):
    """Um erro de digitação em `type` ou `$defs` produz um schema que aceita tudo —
    e os testes de payload abaixo passariam sem verificar nada."""
    schema = json.loads((SCHEMAS / nome).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def payload_real() -> dict:
    event_id = new_event_id()
    return build_event_payload(
        rascunho(event_id), resultado(event_id), identity=IDENTIDADE, reported_at=INSTANTE
    )


def test_payload_real_do_agente_valida(registry):
    validador("event.v1.json", registry).validate(payload_real())


def test_evento_manual_sem_regra_valida(registry):
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id, source=EventSource.MANUAL, rule_id=None, rule_version=None),
        resultado(event_id),
        identity=IDENTIDADE,
        reported_at=INSTANTE,
    )
    validador("event.v1.json", registry).validate(payload)


def test_patch_do_clipe_valida(registry):
    event_id = new_event_id()
    validador("event-clip.v1.json", registry).validate(
        build_clip_patch(
            clip_block(resultado(event_id)), status=ClipStatus.OK, object_key="rede-abc/x.mp4"
        )
    )
    validador("event-clip.v1.json", registry).validate(
        build_clip_patch(
            clip_block(resultado(event_id)), status=ClipStatus.CLIP_FAILED, error="esgotou"
        )
    )


@pytest.mark.parametrize(
    ("nome", "estrago"),
    [
        ("campo obrigatório ausente", lambda p: p.pop("occurred_at")),
        ("event_id que não é uuid", lambda p: p.update(event_id="../../etc/passwd")),
        ("instante sem milissegundos", lambda p: p.update(occurred_at="2026-08-12T18:04:11Z")),
        ("instante em fuso local", lambda p: p.update(occurred_at="2026-08-12T15:04:11.238-03:00")),
        ("status de clipe inventado", lambda p: p["clip"].update(status="talvez")),
        ("campo desconhecido na raiz", lambda p: p.update(frames_b64="…")),
        ("versão de schema errada", lambda p: p.update(schema_version=2)),
        ("regra sem versão", lambda p: p.update(rule={"id": "saida-sem-caixa"})),
        ("tenant vazio", lambda p: p.update(tenant_id="")),
    ],
)
def test_payload_adulterado_e_rejeitado(nome, estrago, registry):
    """Sem os casos negativos, os testes positivos podem estar verdes por vacuidade —
    um schema frouxo aceita tudo, inclusive o que a API vai recusar.

    O caso do `event_id` não é hipotético: esse mesmo id vira nome de arquivo no box,
    e o `ClipStore` já barra travessia de diretório. O contrato barra na outra ponta.
    """
    payload = payload_real()
    estrago(payload)

    with pytest.raises(ValidationError):
        validador("event.v1.json", registry).validate(payload)


def test_campo_novo_no_agente_sem_schema_quebra_alto(registry):
    """`additionalProperties: false` é escolha consciente: a incompatibilidade estoura
    no primeiro evento em vez de descartar em silêncio um campo que alguém achou que
    estava salvando."""
    payload = payload_real()
    payload["track_id"] = 42

    with pytest.raises(ValidationError, match="track_id"):
        validador("event.v1.json", registry).validate(payload)
