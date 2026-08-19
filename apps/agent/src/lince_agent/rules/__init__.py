"""Estágio 4 — motor de regras (§3.4).

O modelo responde *onde* estão as pessoas; quem decide se aquilo é uma possível
ocorrência é este pacote, e a separação é o ADR-002: zonas, tempos e limiares são
configuração por câmera, não constante enterrada no detector. Recalibrar uma loja é
mudar configuração — não retreinar rede, não republicar imagem.
"""
