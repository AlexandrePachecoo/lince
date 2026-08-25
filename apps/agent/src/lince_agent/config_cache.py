"""A última configuração válida, no disco do box (§5.4).

O que a §5.4 exige em uma linha: "`GET /v1/agents/config` falha → mantém a última
configuração válida em cache local e segue operando". Isso vale para o poll em
regime, mas o caso que realmente importa é a **subida**: um box que reinicia — queda
de energia, watchtower, `docker compose up` — com o link da loja caído. Sem cache, ele
não tem configuração nenhuma e não sobe; a loja fica cega justamente no dia em que a
internet está ruim, que não é o dia em que se pode ir até lá.

Três decisões deste módulo, e o que cada uma custa:

1. **Guarda o documento cru, não o `AgentConfig`.** O documento é o que o
   `config_loader` sabe ler e o que `packages/shared/schemas/config.v1.json` define.
   Serializar o `AgentConfig` criaria um terceiro formato — nuvem, documento, cache —
   e o terceiro seria o menos testado dos três. O custo é revalidar a cada subida, que
   é microssegundos uma vez por processo.
2. **Escrita atômica.** Um box que perde energia no meio da gravação não pode acordar
   com um JSON truncado: seria exatamente o cenário do item 1 (sem rede, precisando do
   cache) com o cache destruído.
3. **Só entra o que já passou pelo loader.** Cache de documento inválido é uma loja que
   não sobe mais e cujo conserto exige acesso ao box. O `ConfigPoller` valida antes de
   pedir a gravação, e este módulo não confia nisso: `grava` recebe o documento já
   aprovado e a ordem está documentada em quem chama.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FORMATO = 1
"""Versão do envelope do cache — não do documento. Sobe se o envelope ganhar campo
obrigatório; um cache de formato desconhecido é descartado em silêncio, porque o
agente sempre consegue viver sem ele (busca da nuvem) e nunca consegue viver com um
que ele interpreta errado."""


@dataclass(frozen=True, slots=True)
class ConfigCacheado:
    """O documento da última configuração válida, com o `ETag` que veio junto."""

    documento: dict[str, Any]
    etag: str | None = None
    """O `ETag` da resposta que trouxe este documento. Guardado para que o primeiro
    poll depois de um reinício já possa mandar `If-None-Match` e receber `304` — sem
    ele, todo reinício de agente puxa o documento inteiro de novo."""


class ConfigCache:
    """Um arquivo JSON no disco do box, lido na subida e reescrito a cada mudança."""

    def __init__(self, caminho: Path) -> None:
        self._caminho = caminho

    @property
    def caminho(self) -> Path:
        return self._caminho

    def le(self) -> ConfigCacheado | None:
        """A última configuração válida, ou `None` se não há cache utilizável.

        **Nunca levanta.** Cache corrompido, cache de formato desconhecido, cache sem
        permissão de leitura: todos viram `None` com um log, porque a alternativa —
        derrubar a subida por causa do plano B — inverte a razão de este módulo
        existir. Quem chama já sabe lidar com `None`: vai à nuvem.
        """
        try:
            bruto = self._caminho.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as erro:
            log.warning("cache de configuração ilegível em %s: %s", self._caminho, erro)
            return None

        try:
            envelope = json.loads(bruto)
        except json.JSONDecodeError as erro:
            log.warning("cache de configuração corrompido em %s: %s", self._caminho, erro)
            return None

        if not isinstance(envelope, dict) or envelope.get("formato") != FORMATO:
            log.warning(
                "cache de configuração em %s não é do formato %d; ignorado",
                self._caminho,
                FORMATO,
            )
            return None

        documento = envelope.get("documento")
        if not isinstance(documento, dict):
            log.warning("cache de configuração em %s não tem documento; ignorado", self._caminho)
            return None

        etag = envelope.get("etag")
        return ConfigCacheado(documento=documento, etag=etag if isinstance(etag, str) else None)

    def grava(self, documento: dict[str, Any], *, etag: str | None = None) -> bool:
        """Substitui o cache. Devolve se conseguiu.

        Só deve receber documento que **já passou** por `config_loader.monta_config`:
        gravar um documento que o agente não consegue montar transforma o plano B da
        próxima subida numa falha garantida.

        Falha de escrita — disco cheio, permissão, montagem só-leitura — é `False` com
        log, e não exceção. O agente acabou de aplicar uma configuração válida e está
        funcionando; não conseguir guardá-la para a próxima subida é um problema real,
        mas não é motivo para parar de detectar (§5.4).
        """
        envelope = {"formato": FORMATO, "etag": etag, "documento": documento}
        temporario = self._caminho.with_name(self._caminho.name + ".novo")
        try:
            self._caminho.parent.mkdir(parents=True, exist_ok=True)
            temporario.write_text(
                json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # `os.replace` é atômico no mesmo sistema de arquivos: ou o leitor vê o
            # cache antigo inteiro, ou o novo inteiro. Escrever por cima do arquivo
            # final deixaria uma janela em que ele existe truncado — e é justamente
            # numa queda de energia que essa janela vira o estado permanente.
            os.replace(temporario, self._caminho)
        except OSError as erro:
            log.warning(
                "não consegui gravar o cache de configuração em %s: %s", self._caminho, erro
            )
            # A limpeza do temporário também pode falhar, e falhar aqui seria pior do
            # que o problema original: `missing_ok` só engole `FileNotFoundError`, e
            # um caminho de cache impossível (pai que é arquivo, montagem sumida)
            # levanta `NotADirectoryError`/`PermissionError` no próprio `unlink`. Essa
            # exceção sairia de `grava` e derrubaria o agente **por não ter conseguido
            # salvar o plano B** — exatamente a inversão que a §5.4 proíbe.
            with contextlib.suppress(OSError):
                temporario.unlink(missing_ok=True)
            return False
        return True
