"""Pose da pessoa — piloto de gesto de ocultação.

Recorte top-down, sessão ONNX do RTMPose-s COCO-17 e decode SimCC. O
carregamento segue `detect/onnx.py`: providers, checksum e a trava de nome.

`pose/worker.py` corre o `OnnxPose` fora da thread de detecção e tracking:
uma fila com descarte do mais antigo e uma thread só. O snapshot guarda o
frame, as caixas com `track_id`, o `camera_id` e o `received_at`. Não escolhe
track, não classifica gesto e não entra no runtime — isso é o adaptador, que
ainda não existe.
"""
