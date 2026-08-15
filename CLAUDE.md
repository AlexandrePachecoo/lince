# lince — orientações para trabalhar neste repositório

Detecção de possível furto em mercados de bairro, reaproveitando o CFTV existente.
O sistema **não decide nada sozinho**: produz um alerta com clipe curto e um humano
confirma ou descarta.

Leia [`docs/arquitetura.md`](docs/arquitetura.md) antes de escrever código —
especialmente a §7 (riscos). O maior risco do projeto não é a arquitetura, é a taxa
de falso positivo, e isso muda como cada decisão de implementação deve ser tomada.

Princípio organizador: **a borda decide, a nuvem administra.** Vídeo nunca sai da
loja; o que sobe é o evento (JSON) e o clipe de ~15 s.

---

## Toda função nova precisa de teste automatizado

**Obrigatório, sem exceção.** Código novo sem teste não está pronto, e não importa
se parece trivial. Isto vale para função, método, branch de decisão e correção de
bug.

O motivo é específico deste projeto: quase tudo aqui é **assíncrono, multi-thread e
dependente de processo externo**. Um bug de ingestão não estoura — ele entrega
frames embaralhados, perde um segundo de vídeo em silêncio ou trava o ffmpeg inteiro
sem log nenhum. Bug assim não aparece rodando na mão; aparece três semanas depois,
numa loja, como falso positivo que ninguém consegue explicar.

Regras práticas:

1. **Correção de bug começa pelo teste que falha.** Se o teste não falha antes da
   correção, ele não está testando o bug.
2. **Teste o comportamento e o porquê, não a implementação.** O docstring do teste
   deve dizer o que quebra no mundo real se aquilo regredir — veja
   `tests/test_process.py` como referência de tom.
3. **Nada de mock onde cabe o real.** ffmpeg roda no ambiente; use-o. Mock fica para
   o que é caro ou indisponível: GPU, rede instável, saída de `-hwaccels` num
   servidor sem placa.
4. **Se testar está difícil, o desenho está errado.** Foi assim que
   `CameraSupervisor` ganhou `ingest_factory` e `clock` injetáveis — sem isso, a
   máquina de estados só era testável esperando segundos por transição.
5. **Teste que depende de infraestrutura leva marcador.** Hoje só existe
   `@pytest.mark.rtsp`, excluído por padrão. O resto tem que rodar com
   `uv run pytest` numa máquina limpa.
6. **Nunca use `sleep` para sincronizar teste.** Use relógio falso, `Event` ou
   polling com prazo. Teste que depende de tempo real fica instável em CI.

Antes de dar qualquer coisa por concluída:

```bash
cd apps/agent && uv run ruff check src tests && uv run ruff format --check src tests && uv run pytest
```

---

## Estado atual

| Componente | Situação |
|---|---|
| `apps/agent` | Estágios 1 (ingestão, §3.1), 2 (detecção, §3.2), 3 (tracking, §3.3), 5 (clipe, §3.5) e 6 (fila e envio, §3.6), ligados por `runtime.py`. Falta o motor de regras |
| `apps/api` | vazio |
| `apps/dashboard` | vazio |
| `packages/shared` | JSON Schema do evento e do PATCH do clipe (§5). Heartbeat e config ainda não |

O caminho **gatilho → clipe → fila local → nuvem → clipe apagado do disco** funciona
ponta a ponta, e a borda já vê: o detector emite caixas e o tracker dá a cada pessoa um
ID que atravessa frames. O que ainda não existe é quem **decide** — sem o estágio 4,
quem dispara é andaime (`--trigger-after`, `--trigger-every`, `SIGUSR1`), e os eventos
que ele produz sobem marcados como `source: "manual"` — de propósito, para não
contaminarem a métrica de falso positivo por câmera (R-1).

Onde encostar em cada coisa:

- **Motor de regras (§3.4)** entra em `AgentRuntime._ao_rastrear`, que recebe um
  `TrackingResult` por frame e hoje só passa. A máquina de estados é *por track*, e cada
  `Track` já traz `base_central` (o ponto da travessia), `age_s` e `hits` (o filtro de
  tempo mínimo de vida). Depois é só chamar
  `AgentRuntime.trigger(camera_id, source=RULE, …)` — nada mais precisa mudar para o
  evento chegar à nuvem.
- **Heartbeat (§5.3)** já tem os dados reunidos em `AgentHealth`, agora incluindo
  `inference_fps` e `dropped_frames` por câmera; falta o transporte, que depende do
  registro da §5.1.
- **Download de modelo (§5.2)** substitui o caminho local em `DetectionOptions.model_path`.
  O `model_version` já é nome + checksum, no formato que o ADR-006 quer.

`packages/shared` é onde o contrato da §5 mora em **um lugar só**. Duplicar essa
definição entre a API e o agente é o erro mais caro que dá para cometer neste
projeto. O agente valida o payload real contra o schema em
`tests/test_contrato_evento.py` — é esse teste que impede a divergência.

## Comandos

```bash
pnpm infra:up                 # Postgres e Redis
bash scripts/modelo.sh        # modelo .onnx e vídeo com pessoas (fora do git)
pnpm rtsp:up                  # câmeras RTSP sintéticas (MediaMTX)

cd apps/agent
uv sync
uv run pytest                 # padrão: sem rede, sem câmera, sem infra, sem modelo
uv run pytest -m rtsp         # ponta a ponta; exige `pnpm rtsp:up`
uv run pytest -m redis        # fila local durável; exige `pnpm infra:up`
uv run pytest -m modelo       # o .onnx de verdade; exige `bash scripts/modelo.sh`
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats

# detecção e tracking contra vídeo com pessoas de verdade (cam3):
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
  --model models/yolox_s.onnx --stats

# a verificação do estágio 3 é visual: cada pessoa mantém uma cor no vídeo gravado,
# e cor trocando no meio do percurso é troca de ID (R-2)
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
  --model models/yolox_s.onnx --dump-tracks ./tracks --duration 45 --stats

# caminho inteiro até a nuvem, sem uma API do outro lado:
uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \
  --outbox redis --dry-run --trigger-every 20 --stats
```

## Convenções

- **Código, comentário, docstring, teste e commit em português.** Nomes de
  biblioteca e termos técnicos consagrados ficam como são (`framerate`, `keyframe`,
  `backoff`).
- **Comentário explica o porquê, não o quê.** Se o código diz o quê, o comentário
  diz por que aquela escolha e o que ela custa. Referencie a seção da arquitetura
  (`§3.5`, `R-9`, `NFR-2`) quando a decisão vem de lá.
- Python 3.12 com `uv`, `ruff` para lint e formatação, linha de 100.
- Node 24 com `pnpm`.
- Toda entidade com dado de tenant carrega `tenant_id`, e toda query filtra por ele
  (NFR-6).

## Armadilhas já pagas

Não reintroduza nenhuma destas — cada uma custou depuração e tem teste guardando:

- **`-reconnect` não funciona em RTSP.** É opção de HTTP; passá-la numa entrada
  `rtsp://` não dá erro e não faz nada. Reconexão é código Python.
- **`-fflags nobuffer` e `-use_wallclock_as_timestamps` só valem para fonte ao
  vivo.** Em arquivo não dão erro — perdem frames em silêncio.
- **Todo pipe do ffmpeg tem que ser drenado por uma thread própria.** Um pipe cheio
  bloqueia o processo inteiro, inclusive as outras saídas. Consumidor lento nunca
  pode virar contrapressão: a fila entre leitura e consumo descarta o mais antigo.
- **`-frag_duration` junto com `+frag_keyframe`** produz fragmento que começa no
  meio do GOP e quebra o descarte do buffer circular.
- **`rawvideo` não tem framing.** O tamanho do frame é contrato; errá-lo embaralha
  tudo sem levantar erro.
- **`HTTPError` do `urllib` é levantado _e_ é a resposta.** Tem `.code`, `.headers` e
  `.read()`. Tratá-lo só como exceção joga fora o status, e é o status que decide
  entre reenviar e mandar para a fila morta.
- **`PUT` de arquivo sem `Content-Length` vira `Transfer-Encoding: chunked`**, que o
  R2 recusa numa URL pré-assinada. E o corpo precisa ser **reaberto** a cada
  tentativa: um arquivo já lido está no fim, e o reenvio sobe zero byte sem erro.
- **Score de ZSET 0.0 é falsy.** `if not zscore(...)` tira da fila um item agendado
  para o instante zero, em silêncio. Sempre `is None`.
- **Teste de fila com a nuvem aceitando é corrida.** O sender drena entre o gatilho e
  a asserção. Nos testes de fila o link fica caído por padrão; quem quer o caminho
  completo pede o cliente que aceita, explicitamente.
- **Cada família de modelo tem sua própria convenção de entrada.** YOLOX preenche o
  letterbox no canto **superior esquerdo**, consome **BGR** e recebe **0..255**; a
  linhagem YOLOv5/v8 centraliza, quer RGB e 0..1. Errar qualquer uma das três não dá
  erro: o modelo devolve caixas plausíveis e deslocadas, e o sintoma aparece no §3.4
  como zona errada. É por isso que `PreprocessSpec` é dado explícito e que existe o
  teste de ida e volta em `test_detect_preprocess.py`.
- **A saída do YOLOX não são caixas.** São deslocamentos relativos a uma grade de
  âncoras, com `wh` em log. Sem somar a grade e exponenciar, as caixas saem coladas no
  canto superior esquerdo — e o número de âncoras tem que bater com o `input_size`,
  senão a decodificação é silenciosamente absurda.
- **`np.clip(a[:, [0, 2]], …, out=a[:, [0, 2]])` não escreve nada.** Indexação por
  lista produz cópia; o `out=` aponta para um temporário e o recorte se perde em
  silêncio. Fatia com passo (`a[:, 0::2]`) é vista, e a atribuição funciona.
- **União zero na IoU vira `nan`, e `nan <= limiar` é falso.** A caixa degenerada
  sobrevive a toda supressão, sempre e sem sinal.
- **Vídeo de teste com pessoas não é detalhe.** `testsrc2` é padrão de barras: um teste
  de detecção contra ele concorda com qualquer coisa, inclusive com um detector
  quebrado. A `cam3` existe para isso.
- **A 3 fps, os parâmetros de tracking das implementações de referência não servem.**
  Elas assumem 30 fps. Duas consequências, ambas medidas: o `dt` do Kalman tem que ser
  tempo real (a fila do §3.2 descarta frames, então o intervalo varia), e os limiares de
  IoU precisam ser bem mais frouxos — uma pessoa andando tem IoU de **0,19** entre caixas
  cruas consecutivas. Copiar o ruído de processo diagonal da referência também quebra: ou
  o filtro fica lento demais, ou o ruído de posição explica todo o movimento e a
  velocidade estimada vai a zero.
- **O estado do Kalman não tem restrição, e a caixa vira do avesso.** Quem se afasta da
  câmera encolhe, o filtro aprende altura negativa, e extrapolar durante uma oclusão de
  segundos produz `y2 < y1`. Nada estoura: a IoU dá zero, o desenho some, e a
  `base_central` aponta para o lugar errado. Só apareceu em vídeo real.
- **A caixa de um `Track` não é recortada na moldura** — ao contrário da caixa de uma
  `Detection`. Ela é estimativa, e quem sai pela porta tem os pés estimados além da
  borda; recortar empurraria toda travessia para a beirada do quadro.
- **Detecção fraca nunca cria track.** O piso do detector é 0,10 para alimentar a segunda
  passada do ByteTrack, e é a regra "só continua track existente" que impede isso de
  virar falso positivo.
