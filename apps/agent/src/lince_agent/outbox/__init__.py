"""Estágio 6 — fila local e envio para a nuvem (§3.6).

O que este pacote garante, e que o resto do agente depende para ser útil: **nenhuma
falha de rede pode parar a detecção** (§5.4). O pipeline continua ingerindo,
avaliando e cortando clipes com a internet caída; o que muda é só o tamanho da fila.

O caminho de um evento:

    ClipResult ──> event.py: monta o payload ──> OutboxStore (Redis)
                                                       │
                              policy.py (pura) ──> sender.py: tick()
                                                       │
                                                    http.py
                          POST /v1/events → PUT no R2 → PATCH /v1/events/{id}
                                                       │
                                             ClipStore.discard()  (NFR-3)

A divisão em módulos segue a linha de testabilidade: `policy.py` é pura e roda em
máquina limpa, `store.py` só guarda e devolve, e `sender.py` expõe um `tick()`
síncrono para que o comportamento de rede seja testado sem thread e sem `sleep`.
"""
