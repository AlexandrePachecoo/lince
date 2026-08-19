"""Geometria das zonas do §3.4: polígono de caixa e linha de saída orientada.

Duas primitivas, e as duas existem porque zona errada é falso positivo em massa numa
câmera inteira (R-1) — o modo de falha mais caro do sistema depois da troca de ID.

**Tudo aqui é em coordenadas do frame da câmera**, o mesmo espaço em que
`Detection`/`Track` entregam as caixas e em que o dashboard vai desenhar as zonas
(§4.5). Nenhuma conversão acontece: a caixa já sai do §3.2 com o letterbox desfeito,
justamente para que este módulo e o desenho do operador falem a mesma língua.

O eixo `y` cresce **para baixo**, como em toda imagem. Isso inverte a mão do produto
vetorial em relação à convenção do plano cartesiano, e é a armadilha desta geometria —
ver `LinhaOrientada`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

Ponto = tuple[float, float]


class Travessia(StrEnum):
    """Sentido em que uma linha orientada foi cruzada."""

    SAIDA = "saida"
    """De dentro para fora. É o único sentido que dispara (§3.4)."""

    ENTRADA = "entrada"
    """De fora para dentro. Registrado, e não descartado no meio do caminho, porque
    quem consome precisa distinguir "não cruzou" de "cruzou para o lado que não vale":
    o segundo é sinal de que a linha pode estar desenhada ao contrário."""


@dataclass(frozen=True, slots=True)
class Poligono:
    """Área desenhada sobre o frame — no MVP, a zona do caixa (§3.4).

    Fechado implicitamente: o último vértice liga no primeiro. Convexidade não é
    exigida, porque um caixa de mercado de bairro raramente é um retângulo e obrigar o
    operador a aproximar por um produziria zona errada de propósito.
    """

    vertices: tuple[Ponto, ...]

    def __post_init__(self) -> None:
        if len(self.vertices) < 3:
            raise ValueError(
                f"polígono precisa de ao menos 3 vértices, recebi {len(self.vertices)}: "
                "com dois, a área é zero e a zona nunca conteria ninguém"
            )

    def contem(self, ponto: Ponto) -> bool:
        """Se o ponto está dentro, por lançamento de raio horizontal.

        A comparação de `y` é assimétrica de propósito (`>` de um lado, `<=` do outro).
        É o que faz um vértice ser contado uma vez só: contando nas duas pontas, um
        ponto exatamente na altura de um vértice cruzaria duas arestas e sairia como
        "fora" estando dentro. Como o ponto avaliado são os pés de alguém andando, essa
        coincidência acontece o tempo todo.
        """
        x, y = ponto
        dentro = False
        anterior = self.vertices[-1]
        for atual in self.vertices:
            (x1, y1), (x2, y2) = anterior, atual
            if (y1 > y) != (y2 > y):
                # x da aresta na altura y. `y1 != y2` está garantido pelo teste acima.
                corte = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                if x < corte:
                    dentro = not dentro
            anterior = atual
        return dentro


@dataclass(frozen=True, slots=True)
class LinhaOrientada:
    """A linha de saída (§3.4). Orientada: cruzar de dentro para fora dispara, o
    inverso não.

    **A convenção, em uma frase verificável:** desenhe a linha da esquerda para a
    direita e o **lado de dentro da loja fica embaixo**, isto é, na parte do frame com
    `y` maior.

    Vale a pena conferir a conta, porque a mão está invertida em relação ao que a
    memória do plano cartesiano sugere. Linha de `(0, 100)` a `(100, 100)`, ponto em
    `(50, 200)` — abaixo dela na imagem:

        z = dx·(py − oy) − dy·(px − ox) = 100·(200 − 100) − 0·(50 − 0) = +10000

    Positivo, portanto dentro. Num eixo `y` para cima esse mesmo ponto estaria à
    esquerda do sentido de caminhada; com `y` para baixo, ele está à direita. Errar
    isso não levanta erro nenhum: produz uma câmera que dispara para quem **entra** na
    loja, e o sintoma chega como falso positivo em massa (R-1).
    """

    origem: Ponto
    destino: Ponto

    def __post_init__(self) -> None:
        if self.origem == self.destino:
            raise ValueError(
                f"linha degenerada em {self.origem}: origem e destino coincidem, "
                "e uma linha sem direção não tem lado de dentro"
            )

    def lado(self, ponto: Ponto) -> float:
        """Produto vetorial: positivo dentro, negativo fora, zero em cima da linha."""
        (ox, oy), (dx_, dy_) = self.origem, self.destino
        px, py = ponto
        return (dx_ - ox) * (py - oy) - (dy_ - oy) * (px - ox)

    def dentro(self, ponto: Ponto) -> bool:
        return self.lado(ponto) > 0

    def travessia(self, anterior: Ponto, atual: Ponto) -> Travessia | None:
        """Sentido em que o percurso `anterior → atual` cruzou a linha, ou `None`.

        Duas verificações, e a segunda é a que costuma faltar. A primeira diz que os
        dois pontos estão em lados opostos — mas isso vale para a **reta infinita**, e
        a linha de saída é um **segmento**: alguém andando no fundo da loja, longe da
        porta, atravessa o prolongamento da linha o tempo todo sem nunca chegar perto
        dela. Sem o teste de segmento, essa pessoa dispara um evento.

        Ponto exatamente sobre a linha (`lado == 0`) não conta como travessia; a
        decisão espera o próximo frame, quando ele terá escolhido um lado. Adiar 333 ms
        é mais barato que decidir no ponto ambíguo, que é onde o jitter da caixa
        oscilaria em torno de zero.
        """
        de = self.lado(anterior)
        para = self.lado(atual)
        if de == 0 or para == 0 or (de > 0) == (para > 0):
            return None

        # O percurso separa as duas pontas da linha? Se não, a interseção com a reta
        # caiu fora do segmento desenhado.
        percurso = LinhaOrientada(anterior, atual)
        um = percurso.lado(self.origem)
        outro = percurso.lado(self.destino)
        if um == 0 or outro == 0 or (um > 0) == (outro > 0):
            return None

        return Travessia.SAIDA if de > 0 else Travessia.ENTRADA
