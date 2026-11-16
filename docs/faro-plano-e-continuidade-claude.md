# Faro — plano completo e continuidade no Claude

## 1. Leia primeiro: estado atualizado e precedência

**Handoff de 7 de novembro de 2026, 15:34 (America/Sao_Paulo).** Este documento reúne orientação de continuidade e o plano histórico. Esta primeira parte prevalece sobre afirmações antigas do apêndice. Os fatos abaixo foram fornecidos pelo acompanhamento anterior: esta entrega não consultou GitHub, não rodou testes e não alterou o repositório. Confirme o estado atual antes de trabalhar.

Faro é o nome comercial; o código e o repositório ainda se chamam **lince**: [GitHub](https://github.com/AlexandrePachecoo/lince). Alexandre é fundador solo e sabe programar. Desenvolve no Windows com **RTX 3060 de 8 GB**, pretende rodar na loja com Linux. WSL2 é recomendado para aproximar os ambientes, mas driver NVIDIA, acesso CUDA e desempenho ainda precisam ser conferidos. A referência histórica a uma 3060 de 12 GB não descreve a máquina dele.

**Objetivo:** possível ocultação de produto no corpo/bolsa no corredor. A antiga regra de saída sem caixa não é o produto deste piloto; preservar seu código, mantendo-a desligada nas câmeras do piloto. Nenhum alerta é prova de furto.

### O que já existe

- RTSP/ffmpeg, detecção de pessoas YOLOX, ByteTrack e regra geométrica antiga.
- Buffer e clipes, fila/outbox, API com autenticação, eventos, configuração e heartbeat; dashboard de triagem/métricas.
- Docker CUDA e compose. A afirmação histórica de que não existe Dockerfile está superada; isso não significa instalação validada na loja ou rollback completo.
- Proteções de licença (sem Ultralytics/WholeBody/Halpe), redação de credenciais RTSP, logs e URLs assinadas; CI com MinIO. Os vazamentos descritos no plano são histórico já corrigido, não tarefas a repetir cegamente.
- Pré-processamento RTMPose e decode SimCC, com fixture independente, no PR #10.

### PR #12 — concluído na main

[PR #12](https://github.com/AlexandrePachecoo/lince/pull/12), squash `8dbab6bb65cb489a93f8a5c53b7de0bb75886ba4`: sessão ONNX, export com checksum e ferramenta de benchmark. Preferir a variante **body8**, default confiável com prefixo SHA `acd4a1ef`. Esse prefixo não substitui conferir o SHA completo no repositório. A variante denominada coco foi bloqueada por divergência do prefixo de hash: só aceitar explicitamente com SHA conferido. Não trocar pesos silenciosamente. O provider reportado precisa ser o da sessão real, não a lista de providers compilados/disponíveis.

**O export real ainda não foi executado.** Ferramenta implementada não é artefato real validado.

### PR #13 — concluído e merge verificado

[PR #13](https://github.com/AlexandrePachecoo/lince/pull/13), merge em 7/11/2026 às 15:34 (America/Sao_Paulo), squash `514d2dcdfa87a19b33b9dce3a751452a401e8846`; head revisado `2299e509785b15b63e026a450080a6f585d530f9`.

- `PoseWorker` isolado em thread, fila drop-oldest; snapshots associados a câmera, track, instante e caixa.
- Bytes de `Frame` imutável compartilhados; ndarray copiado para o snapshot.
- Corridas tick/start/stop e tick/tick corrigidas: um único dono da sessão.
- `stop` normal conclui a parada; timeout lança `TimeoutError`, sem fechar sessão em uso. Chamada de parada no callback lança `ParadaReentrante` e exige parada externa.
- Contadores por câmera formam a partição `aceitos = processados + descartados + erros + em_curso + na_fila`; callback falho conta somente como erro.
- Autor informou 1.074 testes passando, 40 deselected e 1 skipped sem cv2. CI GitHub Python/API/dashboard/Docker verde no head revisado; publicação skipped. Esses números não foram reproduzidos localmente nesta entrega.

**Sem deploy. Worker ainda NÃO ligado ao runtime, CLI ou câmeras; sem overlay. Pose real, CUDA e desempenho na RTX nunca foram medidos.** Ter testes sintéticos verdes não demonstra qualidade da mão, vazão ou capacidade por câmera.

## 2. O que falta e em que ordem

1. **Fase 0 ainda não comprovada:** qualificar câmera/substream real, tamanho da pessoa e visibilidade do punho; conferir licença/proveniência dos pesos, consentimento e questões jurídicas. Código pronto não substitui essas evidências. Não declarar a fase encerrada.
2. **Próximo microescopo de código sugerido, sujeito à leitura da main e aprovação:** integração mínima opcional de pose ao tracking confirmado e atualizado, desligada por padrão. Não bloquear detector/ingestão; preservar identidade câmera/track/frame; encerrar e resetar corretamente em reconexão. Testes determinísticos sem pesos reais. Não incluir classificador ou Telegram nesse PR.
3. Overlay no mesmo frame do snapshot, sem desenhar juntas antigas em imagem nova; export real e medição na RTX; documentação reproduzível. Registrar provider real, latência, fps, descartes, fila e memória. Capacidade só depois de medir. Conferir ordem concreta desses pequenos trabalhos no código atual.
4. Dados encenados licenciados/consentidos, negativos difíceis e holdout separado por pessoa e sessão; mídia e poses pessoais fora do git. Janela temporal de 2 s, treino de classificador e export ONNX. Não fabricar pesos, scores ou métricas para preencher o pipeline.
5. Integrar gesto ao evento/configuração/heartbeat, contratos API/dashboard e testes de compatibilidade; abstenção, debounce e reset de identidade.
6. Telegram em sombra só para fundador, tetos diários, expurgo e retenção; depois empacotamento completo, rollout por digest e rollback verificado. Nada de envio, instalação ou deploy implícito.
7. Aplicar os portões do apêndice: câmera (≥50% dos tracks longos com punho utilizável e pessoa ≥180 px); encenação (recall ≥0,60 e precisão ≥0,70 no holdout); sombra (7 dias, ≤15 alertas/dia/câmera, teto duro 20); vigia somente após segundo ciclo e semana seguinte com ≤5 falsos/dia/câmera e recall encenado ≥0,60. São condições, não resultados atingidos. Alvo final de 3 falsos/dia continua distinto do primeiro portão de sombra.

Os prazos do plano são estimativas históricas de engenharia solo, não datas prometidas nem percentuais de conclusão. Jetson, batch, PDV, reconhecimento facial, reidentificação, WhatsApp e Shopformer em runtime continuam fora do próximo escopo.

## 3. O que Alexandre já pode tentar rodar e testar

Roteiro conhecido, **não executado nem validado localmente nesta entrega**. Leia primeiro README, scripts, ajuda da CLI e dependências da main: nomes e flags podem mudar. Windows: preferir ambiente Linux no WSL2 para seguir o roteiro; Linux será o alvo da loja. Conferir integração de rede RTSP e GPU do WSL2 antes de supor equivalência ao Linux nativo.

Ferramentas conhecidas: Python 3.12, uv, Node 24, pnpm 10, Docker e ffmpeg (necessário para os testes que o usam). Não há GPU obrigatória para os testes sintéticos de pose.

No diretório `apps/agent`:

```bash
uv sync
uv run pytest
```

API e dashboard têm testes via `pnpm test` em seus respectivos diretórios; conferir instalação de dependências e serviços no README antes. Não usar banco ou mídia de produção para testes.

Na raiz do repositório, conferir e executar os scripts de modelo YOLOX e RTSP de desenvolvimento:

```bash
bash scripts/modelo.sh
pnpm rtsp:up
```

Depois, em `apps/agent`, com o modelo no caminho esperado (ajuste o caminho conforme a saída do script e a documentação):

```bash
uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 --model models/yolox_s.onnx --dump-tracks ./tracks --duration 45 --stats
```

**Isso gera caixas/rastros, não esqueleto, nem detecção de ocultação.** A sintaxe correta é `--duration 45`, com espaço. O endereço acima é uma fonte local de desenvolvimento, sem credenciais; verifique se o serviço realmente a publicou e se o caminho relativo do modelo existe.

Para GPU, o grupo CPU precisa ser excluído **tanto no sync quanto no run**; conferir driver e compatibilidade CUDA/ONNX Runtime antes:

```bash
uv sync --no-group cpu --group gpu
uv run --no-group cpu --group gpu python -m lince_agent --camera rtsp://localhost:8554/cam3 --model models/yolox_s.onnx --dump-tracks ./tracks --duration 45 --stats
```

Uma execução sem erro não prova uso da GPU: inspecionar provider da sessão real e medir. O Docker já existir também não prova que NVDEC/CUDA funcionem no PC. Não há afirmação de quantas câmeras a RTX 3060 8 GB suporta. Para pose real, ler as instruções do export/benchmark na main antes de inventar comandos; não há artefato exportado ou benchmark validado a entregar aqui.

## 4. Regras para continuidade com Claude / Claude Code

- Leia repo, docs, git e PRs atuais antes de escolher trabalho. Este arquivo é um handoff, não acesso automático ao GitHub nem uma fotografia eterna da main.
- Uma tarefa pequena verificável, branch e PR por vez, com testes de regressão determinísticos. Não sincronizar testes com sleep.
- Não fazer merge, deploy ou force-push sem aprovação específica. A autorização antiga do PR #13 valeu só para ele, não para os próximos. Nesta entrega não iniciar implementação.
- Nunca publicar segredos, tokens, senhas, URLs assinadas ou logs com credenciais. Solicitar configuração por meios apropriados; não copiar valores de fixture para produção.
- Mídia de pessoas, poses e dados derivados ficam fora do git; aplicar consentimento, finalidade e retenção. Não classificar pessoa como criminosa.
- Verificar licenças de código, checkpoint e dados separadamente, fixando checksum completo. Sem Ultralytics/WholeBody/Halpe; RetailS não entra em peso comercial sem licença compatível comprovada.
- Runtime da loja em ONNX Runtime, sem torch, MMPose, MMDetection ou MMCV. Treino/export isolados fora da imagem de runtime.
- Não apresentar estimativa de paper, teste sintético, lista de providers ou build verde como teste real de GPU/câmera/modelo. Registrar o que foi de fato executado e o que continua pendente.

## 5. Como usar e prompt inicial para copiar

Anexe este Markdown ao Claude. No Claude Code, abra o checkout correto e peça para ler o arquivo pelo caminho em que você o salvou. Se a ferramenta não tiver acesso ao repositório/PRs, ela deve dizer o que conseguiu verificar e pedir somente o acesso/contexto necessário; não inventar estado. Preserve este arquivo como referência e mantenha próximos avanços nas docs/PRs do projeto.

> Leia integralmente este arquivo de handoff do Faro, incluindo o apêndice. As ressalvas atualizadas do início prevalecem sobre o plano histórico. O repositório ainda se chama lince. Primeiro confirme o estado atual via git, docs e PRs, especialmente a presença dos PRs #12 e #13 na main. Resuma o que está pronto e pendente sem supor que houve export real, benchmark GPU ou qualificação da câmera. Proponha apenas o próximo microescopo verificável, provavelmente a integração opcional e desligada por padrão do PoseWorker ao runtime/tracking, com testes sem pesos. Não implemente ainda: aguarde meu pedido explícito, Alexandre. Quando eu autorizar implementação, faça uma tarefa pequena em branch/PR; sem merge, deploy ou force-push sem autorização específica. Nunca exponha segredos nem coloque mídia pessoal no git. Diferencie sempre teste sintético, CI e validação real em campo.

---

## Apêndice — plano histórico de gesto de ocultação

**Nota de preservação:** o texto abaixo preserva o plano de origem, exceto por uma única supressão sinalizada de valor literal de token de desenvolvimento. A exigência de não incluir valores de token, mesmo de fixtures, impede reprodução byte a byte desse trecho. Nenhuma outra alteração foi feita no apêndice. Afirmações antigas sobre Docker inexistente, vazamentos ainda abertos, hardware e módulos de pose devem ser lidas com as correções acima. As licenças citadas são registros históricos, não uma nova auditoria nesta entrega.

<!-- INICIO DO PLANO HISTORICO -->
# Plano técnico — piloto de gesto de ocultação no corredor

**Produto do piloto:** detectar alguém escondendo produto no corpo ou na bolsa, no corredor do mercado. A regra “saiu sem passar no caixa” deixa de ser o produto. O código dela permanece, desligado nas câmeras deste piloto.

**Como ler os prazos:** semanas de um desenvolvedor solo, em tempo de engenharia. Gravação com loja parceira estica o calendário sem esticar o código. Nenhum fps de GPU abaixo é medição deste repositório. O único número medido hoje está na §10 de `docs/arquitetura.md`: YOLOX-s a 640 custa cerca de 450 ms por frame em CPU.

**Licenças conferidas em 6 de novembro de 2026**, nas páginas públicas dos repositórios. Código de terceiros muda; o checksum do arquivo que for para a loja é o que vale, no mesmo espírito de `scripts/modelo.sh`.

---

## 1. O que este piloto usa do Lince atual

O caminho que já existe e continua valendo:

```
RTSP → ffmpeg (frame amostrado + fMP4)
     → DetectorWorker (YOLOX, pessoa)
     → TrackerPool (ByteTrack)
     → [novo] fila de pose → RTMPose → janela temporal → classificador
     → runtime.trigger
     → ClipRecorder → fila Redis → POST / PATCH
     → API → Telegram (modo sombra: só o fundador)
```

O gancho está em `apps/agent/src/lince_agent/runtime.py`.

- `_ao_receber_frame` entrega o frame ao `DetectorWorker` e volta na hora. Qualquer coisa lenta aqui enche o pipe e trava o ffmpeg, inclusive o clipe. Isso não muda.
- `_ao_detectar` roda o tracking na mesma thread da inferência, de propósito: o Kalman precisa dos frames em ordem. Isso não muda.
- `_ao_rastrear` hoje chama `RuleEnginePool.update` e, se houver gatilho, `trigger`. O comentário desse método diz a restrição nova: geometria custa microssegundos; pose na mesma thread atrasaria a loja inteira (ADR-007). A pose sai dessa thread.

Peças que se reaproveitam sem reescrever:

| Peça | Onde | Papel no piloto |
|---|---|---|
| Ingestão, reconexão, watchdog | `ingest/supervisor.py`, `ffmpeg/process.py`, `ffmpeg/command.py` | Uma câmera caída não derruba as outras |
| Amostragem no filtro `fps=` | `ffmpeg/command.py` (`sample_fps`) | Sobe a taxa do modelo sem reencodar o clipe |
| Fila drop-oldest | `ffmpeg/queues.py`, `detect/worker.py` | Modelo de fila da pose |
| YOLOX ONNX, só classe 0 | `detect/onnx.py`, `detect/postprocess.py`, `config.py` `DetectionOptions.classes` | Detector de pessoa. Não treina em furto |
| ByteTrack com `dt` em tempo real | `track/bytetrack.py`, `track/kalman.py` | Identidade da janela de 2 s |
| “Track decidido não decide de novo” | `rules/engine.py` (`estado.decidido`) | O mesmo debounce, no gesto |
| Corte, buffer em RAM, remux | `clip/` | Clipe de ~15 s (`ClipOptions` 5 s + 10 s) |
| Fila e contrato do evento | `outbox/`, `packages/shared/schemas/event.v1.json` | O alerta continua JSON + clipe |
| Triagem e métrica | `apps/dashboard`, `apps/api/src/metricas/` | O fundador rotula a sombra. A métrica de falso positivo por câmera continua sendo o número do piloto |
| Classificação quente vs estrutural | `config_diff.py` | Campo novo sem classificação quebra `tests/test_config_diff.py`. Limiar de gesto é quente. Caminho do `.onnx` e `sample_fps` são estruturais |

`sample_fps`, `width` e `height` já estão em `ESTRUTURAL["decode"]`. Mudar a câmera de 640×480 a 3 fps para 1280×720 a 8 fps exige reinício do agente. O poll não aplica isso a quente, e está certo: o ffmpeg e as zonas nascem com esses números.

---

## 2. Câmera de corredor

A câmera atual de projeto (substream 640×480, amostra a 3 fps, `DecodeOptions` em `config.py`) serve para um corpo cruzar uma linha. Para a mão ir à prateleira e ao bolso, ela é a câmera errada. PoseLift (WACV 2025) foi extraído de 1920×1080 a 15 fps. RetailS, de 1080×720 a 15 fps, ângulo alto. Os dois grupos tratam prateleira como oclusão, não como detalhe.

### O que qualifica uma câmera antes de qualquer modelo

Medir no frame que o agente realmente vê (o `scale` do ffmpeg, não o mainstream do DVR):

| Exigência | Número de projeto | Por quê |
|---|---|---|
| Resolução do pipe de detecção | 1280×720 | Abaixo disso o punho vira ruído. 640×480 fica de fora deste piloto |
| Pessoa em pé no meio do corredor | altura da caixa ≥ 180 px; meta 220–320 px | O RTMPose-s come um recorte de 256×192. Caixa de 80 px não tem punho |
| Distância pessoa–câmera | 2,5 a 5 m no trecho útil | Mais longe, a pessoa cabe em 100 px. Mais perto, a câmera vê um palmo de gôndola |
| Trecho coberto | 4 a 6 m de corredor por câmera | Uma câmera não vê a loja. O piloto é um corredor, duas câmeras no máximo no primeiro mês de sombra |
| Ângulo | oblíquo, cerca de 30–50° a partir da vertical, olhando ao longo do corredor | De cima puro some o braço na frente do tronco. De frente, no fim de um corredor longo, a pessoa fica pequena. O ângulo alto dos papers existe para ver a mão na prateleira e o tronco ao mesmo tempo |
| Altura de instalação | 2,5 a 3,5 m | Altura de CFTV de mercado. Mais alto, o corpo vira tampo de cabeça |
| Amostra para o modelo | 8 fps | O gesto dura 0,5 a 2 s. A 3 fps sobram 2 a 6 amostras. A 8 fps sobram 4 a 16. 15 fps é o que os datasets públicos usaram e é folga, não requisito, se a GPU não acompanhar |
| Taxa do clipe | a do substream, sem reencode | O buffer continua sendo H.264 em RAM (`ffmpeg/command.py`). O clipe e o modelo precisam ser o mesmo substream, senão o fundador vê um gesto que o modelo não viu |
| Transporte | TCP, já é o padrão (`rtsp_transport`) | UDP em LAN de mercado pixeliza justo na mão |
| Luz | o corredor como ele é às 18h, não só ao meio-dia | Contraluz de porta e lâmpada falha são os frames em que o punho some |

Checklist de recusa, no espírito do R-3: se, numa gravação de 10 minutos com gente real, menos da metade dos tracks com mais de 1 s tiver punho com confiança decente, a câmera não entra no piloto. Mexer no limiar do classificador não corrige ângulo.

O DVR precisa entregar um substream de 720p. Muitos NVRs de bairro só oferecem 640×480 no substream e 1080p no main. Nesse caso o piloto usa o main só das duas câmeras do corredor, e nenhuma outra. Oito mains 1080p no mesmo box não é o desenho.

Zonas continuam úteis, com outro sentido. Um polígono “gôndola vigiada” restringe o classificador a quem está na frente da prateleira. Quem só atravessa o corredor não gera janela. Isso reaproveita `rules/geometry.py` (ponto no polígono) sem usar a linha de saída. Vértices fora de `decode.width/height` já são recusados na subida. Redesenhar a zona é obrigatório ao mudar a resolução, porque a zona antiga fica no quadro errado e passa a não conter ninguém.

---

## 3. Pipeline

### 3.1 Detecção — manter

`OnnxDetector` + `decode_yolox` + letterbox do YOLOX (canto superior esquerdo, BGR, 0..255). O arquivo continua sendo o YOLOX-s oficial da Megvii, Apache-2.0, checksum em `scripts/modelo.sh`.

Não fine-tunar o YOLOX em “furto”. Ele responde onde está a pessoa. Fine-tune de detector só entra se, na câmera qualificada, a pessoa atrás da gôndola sumir com frequência. Aí o dado é caixa de pessoa, não gesto.

O piso `score_threshold = 0.10` e o `high_threshold = 0.5` do ByteTrack ficam. Pose só roda em track que já passou do limiar alto (`Track` confiável, `time_since_update_s == 0`). Detecção fraca serve para não quebrar o ID. Pose em caixa fraca queima GPU e ensina o classificador com esqueleto de sombra.

### 3.2 Tracking — manter, recalibrar depois de medir

`track/bytetrack.py` já usa tempo real no Kalman e IoU frouxo porque a 3 fps uma pessoa andando tem IoU ~0,19 entre caixas cruas. A 8 fps o deslocamento cai e aquele gate fica largo: em corredor cheio, dois vizinhos trocam de ID com mais facilidade.

O piloto é só câmera de corredor, então um único bloco `tracking` no documento da loja (`config.v1.json`, hoje no nível da loja, não da câmera) basta. A ordem é: gravar `--dump-tracks` a 8 fps no corredor real, olhar troca de cor no meio do percurso, e só então apertar `iou_min`. Trocar o limiar é campo quente (`config_diff.py`): não zera o Kalman. Zerar track continua sendo o que `RuleEngine.reconfigure` faz quando a zona muda, e o gesto precisa da mesma regra ao mudar o polígono da gôndola.

A janela do gesto é curta (2 s). Uma troca de ID estraga uma janela, não a visita inteira. Ainda assim o classificador exige `age_s` mínimo (1 s) e punhos visíveis, para não decidir no track que acabou de nascer.

### 3.3 Pose — módulo novo

Código novo, no padrão do detector:

```
apps/agent/src/lince_agent/pose/preprocess.py   # recorte, 256×192, convenção do RTMPose
apps/agent/src/lince_agent/pose/onnx.py         # sessão ONNX, SimCC, checksum
apps/agent/src/lince_agent/pose/worker.py       # fila própria, drop-oldest, uma thread
apps/agent/tests/test_pose_preprocess.py
apps/agent/tests/test_pose_onnx.py
apps/agent/tests/test_pose_worker.py
```

Modelo: **RTMPose-s, COCO-17, ONNX**, exportado do MMPose. O código do MMPose e do RTMPose é Apache-2.0 (resposta dos mantenedores na issue 2393 do MMPose). O COCO de 17 pontos é CC BY 4.0, uso comercial com atribuição.

Ficam de fora, de propósito:

- Checkpoint Halpe / COCO-WholeBody. O COCO-WholeBody é não comercial. A issue 65 do `rtmlib` registra a resposta dos autores: peso treinado nele não vai para produto pago sem licença comercial.
- `rtmlib` como dependência do box. Serve de referência para o export. O processo da loja fala com ONNX Runtime, como `detect/onnx.py`. MMDetection, MMCV e PyTorch não entram na imagem.
- Qualquer pose da linhagem Ultralytics (YOLO-pose incluso).

O pré-processamento do RTMPose não é o letterbox do YOLOX. É um recorte da caixa da pessoa, razão 192×256 na convenção do checkpoint, RGB, normalização do treino. Errar canal ou média produz juntas plausíveis e deslocadas, o mesmo fracasso silencioso descrito em `detect/preprocess.py`. O teste de ida e volta é obrigatório: junta conhecida no frame → tensor → junta de volta.

Saída SimCC: o ONNX do RTMPose devolve mapas de classificação, não coordenadas prontas. O decode mora em `pose/onnx.py` e tem teste com tensor fixo. Sem isso o primeiro frame na loja é o primeiro lugar em que o formato aparece.

Custo: a pose não roda em todo frame vazio. Roda nos tracks confirmados daquele frame. Duas câmeras, 8 fps, quatro pessoas no corredor = 64 recortes/s no pico. A fila da pose descarta o recorte mais antigo e nunca bloqueia a thread de detecção. O snapshot enfileirado carrega `camera_id`, `track_id`, `received_at` e a caixa. Sem o `track_id` já atribuído, o esqueleto não tem com quem acumular tempo.

`providers` ganha `TensorrtExecutionProvider` na frente de `CUDAExecutionProvider`, no mesmo mecanismo de `detect/onnx.py`: o heartbeat diz qual pegou. Pedido não é obtido. O caminho do `.onnx` de pose é flag do box, como `--model`, e não campo que a nuvem possa apontar para um disco arbitrário. Quando o download de modelo existir, ele baixa o arquivo e o checksum; não escolhe o diretório.

### 3.4 Classificador temporal — módulo novo, modelo pequeno

```
apps/agent/src/lince_agent/gesture/janela.py     # anel de 2 s por track, normalizado no tronco
apps/agent/src/lince_agent/gesture/classificador.py  # ONNX de um cabeçote pequeno
apps/agent/src/lince_agent/gesture/engine.py     # limiar, abstenção, debounce
apps/agent/tests/test_gesture_janela.py
apps/agent/tests/test_gesture_engine.py
```

Entrada da janela, por track:

- 2,0 s, passo de 0,5 s.
- 17 pontos COCO, `x` e `y` relativos ao centro do quadril, divididos pela escala do tronco, mais a confiança.
- Confiança 0 e coordenada 0 quando a junta falta. Sem interpolar buraco longo: punho invisível por mais de 0,4 s na janela é abstenção, não “normal” e não “alerta”.
- O anel zera quando o track some ou a câmera reconecta. `RuleEngine.reset` já existe para o caso do ID recomeçar em 1. O gesto precisa do mesmo gancho no `TrackerPool`.

Saída do piloto: **uma classe, ocultação**, com o tipo (bolso, bolsa, sob a roupa) guardado como rótulo de treino e, se o cabeçote for multiclasse, como campo do evento. A decisão que dispara é binária. Três subclasses com poucas dezenas de exemplos cada uma não seguram um limiar.

Cabeçote: uma rede temporal pequena (convolução 1D ou transformer de poucas camadas) sobre a janela já normalizada. Treina fora da loja. Exporta ONNX. O box não vê PyTorch.

**Shopformer não é o modelo do box.** O repositório `TeCSAR-UNCC/Shopformer` é Apache-2.0 e implementa um autoencoder de pose (CVPRW 2025): erro de reconstrução vira “anomalia”. Isso dispara para qualquer movimento raro (agachar, mexer no celular, amarrar o sapato), e o artigo do RetailS lembra que esse escore não é probabilidade. Uso certo do Shopformer neste plano: baseline offline, no mesmo conjunto de validação, para saber se o cabeçote supervisionado perde de um método publicado. Portar o GCAE para o agente é outro projeto.

**PoseLift** (`TeCSAR-UNCC/PoseLift`, licença Apache-2.0 no GitHub): 153 trechos, 43 de furto e 112 de compra, 6 câmeras, poses já extraídas, sem vídeo. Serve como pré-treino ou conjunto auxiliar. Não serve como métrica do Lince. As poses foram extraídas com YOLOv8 e HRNet, noutro país, a 15 fps e 1080p. O esqueleto publicado pode ser usado segundo a licença do repositório. O pipeline de extração deles, com Ultralytics, não entra aqui. Há deslocamento de domínio: junta de HRNet não é junta de RTMPose-s. Antes de misturar, rodar o RTMPose-s do Lince não dá, porque eles não soltam o vídeo. Tratar PoseLift como sinal fraco, ou reextrair só se um dia o vídeo estiver licenciado.

**RetailS** (`TeCSAR-UNCC/RetailS`): o README descreve poses COCO-17, 15 fps, 1080×720, ~20 milhões de frames normais, teste encenado e teste real, e a métrica HPRS (média harmônica de precisão, recall e especificidade). O README público consultado não traz arquivo de licença. Até existir `LICENSE` compatível com produto fechado, o RetailS não entra no peso que vai para a loja. A ideia do HPRS entra: na hora de escolher o limiar, F1 sozinho ignora o alarme falso.

Gestos que o conjunto de treino tem que conter como **negativo**, senão o produto morre na primeira tarde:

- pôr o produto na cesta ou no carrinho
- devolver o produto à prateleira
- ler o rótulo, agachar, coçar, ajeitar a própria bolsa
- guardar o próprio celular no bolso

O último é o falso positivo que parece um acerto no papel.

### 3.5 Onde o gatilho entra

`GestureEngine` devolve um gatilho no mesmo formato que o `RuleEngine` já devolve (câmera, instante, id da regra, versão). `runtime.trigger` segue sendo o único lugar que cria `event_id` e pede clipe.

Contrato, em `packages/shared/schemas/event.v1.json`:

- `source` ganha o valor `gesture`, ao lado de `rule` e `manual`.
- Bloco opcional `gesture`: `id` da regra (`ocultacao`), `version`, `score`, `janela_s`, `punho_visivel` (fração), `model` (nome@checksum, os 12 hex que `model_version` já usa).
- `rule` continua para a linha de saída.

Isso pede o enum `EventoOrigem` em `apps/api/prisma/schema.prisma`, o validador da API, `apps/dashboard/src/api/tipos.ts` e `apps/dashboard/test/contrato.test.ts`. Sem o valor novo, o gesto cai em `rule` e contamina a métrica no dia em que alguém religar a linha de saída.

`versions.model` passa a ser uma string composta, estável e curta: `yolox_s@…+rtmpose_s@…+gesto@…`. O heartbeat (`heartbeat.py` e `packages/shared/schemas/heartbeat.v1.json`) ganha, por câmera: `pose_fps`, `gestos`, `abstencoes`, `punho_baixo`. É o equivalente de `criados_por_minuto` para este piloto: se o punho não aparece, o problema é a câmera.

Limiares do gesto (`score_min`, `punho_min`, `altura_min_px`, `cooldown_s`, `enabled`) entram em `A_QUENTE`. Trocar o arquivo ONNX, o tamanho da janela ou o `input` da pose entra em `ESTRUTURAL`. `tests/test_config_diff.py` tem que falhar se algum campo ficar de fora. Na dúvida, estrutural, como o próprio módulo manda.

A regra de saída fica com `rules.enabled: false` nas câmeras do piloto (`config.exemplo.json` e o seed). O módulo `rules/` e os testes dele continuam. Religar é configuração, no dia em que um piloto de porta fizer sentido.

---

## 4. Dataset, treino e métrica

### 4.1 Três fontes, três papéis

| Fonte | Papel | O que não fazer |
|---|---|---|
| Gravação encenada no corredor alvo, com o mesmo DVR, a mesma resolução e os mesmos 8 fps | Conjunto principal. É o único que tem o domínio da loja | Não gravar em casa com webcam e esperar o mesmo escore |
| Sombra na loja parceira | Negativos difíceis e a métrica verdadeira (alarmes por dia) | Não gravar o dia inteiro em disco. Só o clipe do alerta e, no máximo, uma amostra curta de quase-alertas |
| PoseLift, e RetailS só com licença escrita | Pré-treino e comparação com a literatura | Não citar o AUC deles como número do Lince. Não commitar vídeo nem JSON de pessoa no git (`.gitignore` já recusa mídia; pose de marcha também identifica) |

Encenação, protocolo mínimo para um corredor:

- 3 pessoas, roupas diferentes, com e sem bolsa, duas visitas de 30–40 minutos.
- Roteiro cronometrado: cesta, devolve, lê rótulo, celular no bolso, bolso, bolsa, sob a blusa. Cada gesto, 20 vezes, dos dois lados do corredor.
- Termo de consentimento dos atores. Eles sabem que estão sendo filmados. Cliente real não entra nessa gravação: o corredor fecha, ou a gravação é fora do horário, ou a câmera é uma gôndola montada num estoque.
- Rótulo por intervalo de tempo e track, não por frame solto: início, fim, classe. Um CSV ao lado do clipe basta. A tela de triagem do dashboard entra na sombra, não na encenação.

Sintético (pessoa 3D, jitter de junta, apagar punho, entortar o tempo) só aumenta o treino. Não entra no número que decide o limiar. Oclusão de prateleira de verdade o sintético não imita.

Volume que justifica um cabeçote pequeno: algumas centenas de janelas positivas encenadas e várias vezes isso de negativas, com pessoas diferentes no treino e no teste. A divisão é por pessoa e por sessão. Dividir por frame vaza o mesmo gesto para os dois lados e o escore mente.

### 4.2 Treino

Fora da loja. O box só recebe três arquivos `.onnx` com sha256, no mesmo ritual de `scripts/modelo.sh`.

1. YOLOX-s congelado.
2. RTMPose-s COCO-17 congelado. Sem fine-tune de pose: não há junta anotada à mão, e fine-tune sem isso piora o punho.
3. Cabeçote temporal treinado nas janelas do RTMPose-s da encenação. PoseLift pode inicializar, com um teste ablativo: com e sem ele, no holdout encenado. Se não subir o escore, fica de fora, para o peso da loja ter uma procedência só.

O otimizador, a augmentação de junta e o export ONNX vivem num diretório de treino separado (`research/gesto/`, se for para o repo, só código e testes sintéticos; os clipes ficam fora do git). O agente não importa esse diretório.

### 4.3 Métrica e limiar

Unidade: **alerta**, não frame. Um alerta é verdadeiro se a janela cruza um intervalo rotulado como ocultação daquela pessoa. Dois alertas no mesmo gesto contam uma vez (o debounce do track já existe para isso).

Reportar, sempre por câmera:

- precisão e recall no holdout encenado, pessoas que o treino não viu
- alarmes falsos por hora, e por dia de loja aberta, na sombra
- taxa de abstenção (punho ruim ou pessoa pequena)
- HPRS no conjunto de validação, para escolher o limiar. F1 sozinho empurra o limiar para baixo e a sombra vira enchente

Portões, nesta ordem. Nenhum deles é promessa de produto; são as condições para passar de fase:

| Portão | Condição para avançar |
|---|---|
| Câmera | ≥ 50% dos tracks longos com punho utilizável; pessoa ≥ 180 px |
| Encenação | recall ≥ 0,60 e precisão ≥ 0,70 no holdout de outras pessoas, com o limiar que maximiza HPRS |
| Sombra, 7 dias de loja aberta, 1 corredor | ≤ 15 alertas/dia/câmera para o fundador, teto duro no código em 20, e o fundador consegue assistir o clipe e marcar a triagem |
| Vigia | depois de um segundo ciclo de treino com os falsos da sombra: ≤ 5 falsos/dia/câmera numa semana seguinte, recall encenado ainda ≥ 0,60 |

O teto de 3 falsos/dia da NFR-2 continua sendo o alvo do produto. Não é o critério da primeira sombra. A métrica `GET /v1/metricas/falso-positivo` já julga o pior dia, no fuso da loja. O piloto usa essa rota. `inconclusivo` continua fora do falso positivo, como já está implementado.

Cooldown por track: 60 s. Teto diário por câmera, aplicado na API antes do Telegram, no mesmo espírito do R-12. Estourar o teto não apaga o evento: ele fica na fila do dashboard para o fundador ver em lote.

---

## 5. Hardware e vazão

Dois alvos. O primeiro piloto não usa os dois ao mesmo tempo.

| | Mini PC com RTX 4060 (8 GB) ou 3060 (12 GB) | Jetson Orin Nano Super (8 GB, 67 TOPS, ~25 W) |
|---|---|---|
| Papel neste plano | Máquina de desenvolvimento e do primeiro corredor | Porte depois que o ONNX está congelado e a vazão foi medida |
| Runtime | `onnxruntime-gpu` com CUDA do host, ou TensorRT EP | Build do JetPack. O pacote `onnxruntime` do `pyproject.toml` é o de CPU e não serve |
| Decode | NVDEC, `HwAccel.CUDA` já previsto em `ffmpeg/command.py` | NVDEC do Jetson. A imagem é outra: ffmpeg compilado para aquele JetPack |
| Conforto esperado, a confirmar na semana 1 | 4 câmeras × 8 fps de YOLOX-s, pose só nos tracks | 2 câmeras × 8 fps. 4 câmeras é a pergunta da medição. 8 câmeras não é meta |
| Por que | Folga de memória e o mesmo Linux em que o fundador depura | Cabe na loja, gasta pouco, trava o calendário em empacotamento |

Orçamento até existir medição, de propósito largo:

- O detector é o custo dominante. YOLOX-s a 640, TensorRT FP16, fica na casa das dezenas de inferências por segundo no Orin Nano e bem acima disso numa 4060. A §10 já mostrou que em CPU não cabe uma câmera. Não usar a faixa de “centenas de fps” de blog sem rodar o arquivo exato do `modelo.sh`.
- RTMPose-m no README oficial passa de 430 fps numa GTX 1660 Ti, no recorte, não no vídeo inteiro. Um estudo de 2025 no Orin Nano mediu cerca de 10 fps com PyTorch e tempo real só depois do export (MMDeploy). Por isso o box não roda PyTorch, e a pose é RTMPose-**s**, não **m**.
- Conta do piloto: 2 × 8 = 16 detecções/s, mais até ~64 poses/s com quatro pessoas. É o ponto de projeto do Orin. 4 × 8 = 32 detecções/s é o ponto de projeto da 4060. Oito câmeras de corredor exigem batch, que `detect/worker.py` ainda não faz (`§10.2` deixou isso para quando houvesse GPU).

O que medir no primeiro dia de GPU, e gravar na §10 no lugar destas faixas: ms de YOLOX-s, ms de RTMPose-s por pessoa, `inference_fps`, profundidade da fila, RAM do buffer de 30 s a 720p. O buffer comprimido continua barato em relação ao raw; 720p aumenta os dois, e o raw não é o que fica em RAM.

Recomendação: o corredor piloto roda no mini PC com RTX. O Orin entra como segunda imagem quando o grafo ONNX não muda mais. Otimizar TensorRT em cima de um cabeçote que ainda vai ser retreinado toda semana é calendário perdido. O EP do TensorRT pode ser tentado na 4060 na semana 1; se o ganho for pequeno perto do CUDA EP, fica para o porte do Jetson, onde ele faz diferença.

Memória: 8 GB no Orin são da GPU e do sistema juntos, com ffmpeg e Redis no mesmo aparelho. Por isso 2 câmeras, não 8.

---

## 6. Alerta, sombra, LGPD, senha no log, YOLOV8

### 6.1 Telegram e modo sombra

O token do bot fica na API (`apps/api/.env`), não no box. O box já manda o evento e o clipe. A nuvem decide quem recebe. Um HTTP de configuração não ganha o poder de escolher o chat, pelo mesmo motivo que `--api-token` não vem no documento da loja.

Fluxo:

1. A borda faz o que já faz: `POST /v1/events`, `PUT` do clipe, `PATCH`.
2. Quando `clipeEstado` passa a `disponivel` (`apps/api/src/routes/events/clip.ts`), a API envia o vídeo ao Telegram.
3. `Loja.modoAlerta`: `sombra` ou `vigia`. Na sombra, o `chat_id` é o do fundador. No vigia, é o do grupo da loja. A troca é coluna, não deploy.
4. Texto fixo: “possível ocultação”, câmera, horário, escore. A palavra furto não entra na mensagem. O R-10 já pede isso.
5. Teto diário por câmera antes do envio. Evento acima do teto fica na fila do dashboard.
6. O bot apaga a mensagem depois de 24 h (`deleteMessage`), porque a retenção do Telegram não obedece a política da loja. O clipe no bucket obedece o expurgo abaixo.
7. Falha de envio não perde o evento. A triagem no dashboard continua sendo o registro. Telegram é notificação.

O clipe sai da loja duas vezes: para o bucket (já é o desenho) e para os servidores do Telegram, que podem estar fora do Brasil. Isso é transferência internacional. O controle de engenharia é sombra, prazo curto e apagar a mensagem. A base legal e o registro dessa transferência são com advogado, antes da primeira loja com cliente real. Encenação com atores que consentiram pode usar Telegram antes disso.

O dashboard não sai. No piloto ele é a ferramenta de rótulo do fundador (`/fila`, `/eventos/:id`, três botões). O vigia não é usuário dessa tela até o portão da seção 4.3. WebSocket e WhatsApp continuam fora.

### 6.2 Expurgo

Hoje o clipe local some depois do upload confirmado (fila), e o objeto no bucket fica. `storage/object-key.ts` já põe a data de `occurred_at` na chave. `POLITICA_RETENCAO` está nomeada no schema e não existe como tabela.

Acrescentar:

- Tabela `politica_retencao`: `loja_id`, `clipe_horas` (piloto: 48), `evento_dias` (piloto: 30, sem o vídeo).
- Job na API, de hora em hora: lista objetos com `clipeEstado = disponivel` mais velhos que o prazo, apaga no bucket, marca o estado como expirado (valor novo, ou reutiliza `indisponivel` com um motivo `expurgo` — melhor um valor explícito para a tela não dizer “não houve vídeo”).
- Regra de ciclo de vida no bucket, com o mesmo prazo, como segunda trava. As duas têm que concordar, senão a tela aponta para um objeto que a regra do bucket já apagou, ou o contrário.
- Pose e escore que tenham ido parar no JSON do evento seguem o prazo do evento. Não há tabela de esqueleto contínuo. O anel de 2 s vive na RAM do agente e morre com o track.
- Teste de API com MinIO real, no estilo de `apps/api/test/storage/`.

48 horas é curto o bastante para o piloto e longo o bastante para o fundador ver no dia seguinte o que a sombra disparou à noite. Aumentar esse prazo é decisão consciente, não omissão.

### 6.3 Senha RTSP no log

A URL pode carregar usuário e senha. Hoje ela vaza em três pontos:

- `ingest/supervisor.py` grava o argv inteiro do ffmpeg, e `command.py` põe a URL em `-i`.
- `ffmpeg/probe.py` coloca a URL na `ProbeError`.
- O mesmo argv iria para qualquer log futuro de “comando que falhou”.

Correção: uma função pura `redige_credencial(url)` em `ffmpeg/command.py`, que troca `user:password@` por `***@` e deixa esquema, host e caminho. O supervisor loga o argv já redigido. O probe usa a URL redigida na exceção. Teste em `tests/test_command.py`: a senha não aparece; host e caminho aparecem. Conferir também o stderr do ffmpeg (`log_level` já é `warning`): se algum aviso ecoar a URL, o callback `on_log` passa pela mesma função antes de ir para o logger.

### 6.4 Remover o spec YOLOV8

`detect/preprocess.py` define `YOLOV8 = PreprocessSpec(centered=True, to_rgb=True, normalize=True)` “para o dia em que o modelo alvo mudar”. Esse dia, com peso da Ultralytics, é AGPL-3.0 num produto fechado. A licença enterprise da Ultralytics existe para esse uso. A licença MIT do ONNX Runtime não cobre o peso.

Remover a constante `YOLOV8` e os testes que montam tensor YOLOv8 em `tests/test_detect_preprocess.py`. Manter `PreprocessSpec` e a constante `YOLOX`. Manter um teste que prova o default: canto superior esquerdo, BGR, faixa 0..255. Acrescentar um teste pequeno que recusa nome de arquivo com `yolov8`, `yolo11` ou `ultralytics` na subida do `OnnxDetector`, com mensagem que cita a licença. Um peso Apache com outra convenção de entrada, no futuro, ganha uma constante nova com a licença escrita do lado, não um atalho genérico “família YOLO”.

---

## 7. Empacotamento da loja

Não existe Dockerfile. `infra/docker-compose.yml` sobe Postgres, Redis e MinIO de desenvolvimento. O box da loja não leva Postgres.

Dois artefatos:

- `apps/agent/Dockerfile` com alvo `cuda` (x86, ffmpeg com NVDEC, `onnxruntime-gpu` pinado na CUDA da imagem).
- O mesmo Dockerfile, alvo `jetson`, só quando o porte começar. Base L4T do JetPack correspondente ao Orin Nano Super. Não dá para buildar essa imagem num notebook x86 e esperar que rode; o CI dela é o próprio aparelho, ou um runner ARM.

`infra/docker-compose.box.yml`:

- `redis`, com `appendonly yes`, como a ADR-004 já assume. Volume nomeado.
- `agent`, `restart: unless-stopped`, GPU reservada, rede só de saída (API, bucket, DNS). Sem porta publicada na LAN além do que o Tailscale precisar.
- Arquivo de ambiente no aparelho, fora do git: URL RTSP, token do agente, caminho dos modelos. O documento de zonas continua vindo da API (`--config-nuvem`) ou, na primeira instalação, de um JSON local.

Alternativa se a loja não tiver quem olhe um compose: duas units systemd, `lince-redis.service` e `lince-agent.service`, com o mesmo binário. Uma das duas formas, não as duas ao mesmo tempo no mesmo aparelho.

Atualização remota:

- Imagem pinada por digest, não por `:latest`.
- Script no box: baixa o digest que a API publica para aquela loja, sobe o container novo, espera o heartbeat com a versão nova (`heartbeat.py` já manda a versão do agente), e volta ao digest anterior se o heartbeat não aparecer em 2 minutos.
- Uma loja por vez. A §7 (R-7) já diz que atualizar todas juntas é visita presencial.
- Watchtower apontando para `latest` fica de fora. Ele atualiza no escuro, que é o cenário do R-7.
- Troca de modelo é arquivo + checksum + reinício, porque `model_path` é estrutural. Não se mistura com troca de limiar, que é quente e não reinicia.

Acesso: Tailscale, como a §3.8, para não abrir porta no roteador do mercado. O token do agente que o `scripts/seed.ts` semeia (`[TOKEN DE DESENVOLVIMENTO SUPRIMIDO]`) não vai para esse aparelho. Registro pelo `POST /v1/agents/register`, que a API já tem.

---

## 8. O que fica, o que desliga, o que nasce

**Fica em uso**

- Ingestão, clipe, fila, contrato, API de evento, autenticação, heartbeat, config com ETag, dashboard como bancada de rótulo, métrica de falso positivo, YOLOX, ByteTrack, testes desses módulos.

**Desliga no piloto, sem apagar código**

- `rules.enabled = false` nas câmeras de corredor. Linha de saída e zona de caixa continuam no schema para um piloto futuro de porta.
- Gatilho manual (`--trigger-every`, SIGUSR1) continua para testar instalação, sempre `source: manual`.

**Nasce**

- `pose/`, `gesture/`, campo `gesture` no evento, `source: gesture`.
- Telegram na API, coluna de modo da loja, teto diário.
- `politica_retencao` e o job de expurgo.
- Redação da credencial RTSP.
- Remoção do spec `YOLOV8`.
- Dockerfile e compose do box.
- Diretório de treino fora da imagem do agente.

**Continua adiado**

- WebSocket, WhatsApp, desenho de zona no dashboard, download de modelo pela API (até o ONNX parar de mudar toda semana; enquanto isso, arquivo no disco com checksum), batch entre câmeras, PDV, reidentificação, reconhecimento facial.
- Porte Jetson, até o portão da sombra na RTX.
- Shopformer dentro do processo da loja.
- RetailS dentro do peso, enquanto a licença não estiver escrita.

---

## 9. Fases

Cada fase termina com teste automático do que for código, no critério que o repositório já usa: `uv run pytest` no agente, `pnpm test` na API, `pnpm test` no dashboard. Teste novo descreve o que quebra na loja se regredir. Sem `sleep` para sincronizar. Clipe de pessoa não entra no git.

### Fase 0 — câmera, licença e loja (1 semana)

Sem modelo novo. Visitar o corredor, gravar 10 minutos do substream real, medir altura da pessoa em pixels e se o substream é 720p. Ler a licença do checkpoint RTMPose-s escolhido e guardar o SHA. Combinar com a loja a encenação fora do horário de cliente, ou fechar o corredor. Falar com advogado sobre aviso na loja e sobre o Telegram antes de cliente real aparecer em clipe. Critério de saída: uma câmera passa na tabela da seção 2, ou o piloto muda de corredor. Se nenhuma câmera passar, as fases seguintes não começam.

### Fase 1 — ver a mão (2 semanas)

- Subir `sample_fps` e a resolução no documento da câmera (reinício, campo estrutural).
- Overlay de pose no mesmo espírito de `--dump-tracks` (`track/draw.py`): juntas no vídeo, para o olho julgar.
- Módulos `pose/` com ONNX e testes de pré-processamento e de decode SimCC.
- Medir ms no mini PC com RTX. Anotar na §10.
- Redigir a senha RTSP.
- Remover `YOLOV8`.
- TensorRT EP se a medição mostrar fila cheia com duas câmeras; caso contrário, CUDA EP nesta fase.

Critério de saída: duas câmeras a 8 fps, pessoa ≥ 180 px, punho visível na maior parte dos tracks, fila de detecção sem descarte contínuo, senha ausente do log.

### Fase 2 — encenação e rótulo (2 semanas de calendário, ~1 de código)

Protocolo da seção 4.1. Script que lê o clipe, roda o mesmo ONNX do agente e escreve janelas + CSV. A divisão por pessoa fica registrada no CSV. Nenhum dado no git.

Critério de saída: holdout de uma pessoa que não está no treino, com as contagens da seção 4.3 calculáveis.

### Fase 3 — cabeçote e fio no runtime (2 semanas)

Treino, export ONNX, `gesture/engine.py` chamando `runtime.trigger` com `source: gesture`. Testes com janelas sintéticas (números, não vídeo): punho ausente não alerta; score alto alerta uma vez; reconexão zera o anel; campo novo no `config_diff` classificado. Schema, Prisma, dashboard mostrando o escore no evento.

Critério de saída: o portão de encenação da seção 4.3, num teste que roda sem GPU (o ONNX do cabeçote é pequeno; o de pose pode ficar marcado `@pytest.mark.modelo`, como o YOLOX já é).

### Fase 4 — sombra num corredor (2 semanas)

Compose do box, registro do agente sem o token de dev, Telegram só para o fundador, teto diário, expurgo de 48 h, uma semana de loja aberta. O fundador marca a fila.

Critério de saída: o portão de sombra da seção 4.3. Se estourar 15/dia, o piloto continua em sombra e volta para a fase 3 com os clipes falsos. Não se chama o vigia.

### Fase 5 — segundo ciclo e, se couber, o vigia (3 a 4 semanas)

Retreino com os falsos reais (celular no bolso, cesta, bolsa ajeitada). Nova semana de medição. Porte para o Orin só se a loja for usar esse hardware e a fase 4 tiver passado na RTX: a meta é repetir as 2 câmeras × 8 fps, não inventar uma terceira arquitetura.

Critério de saída para ligar `modoAlerta = vigia`: o portão da seção 4.3. Sem ele, o produto fica em sombra. Sombra que o fundador não consegue acompanhar também não segue: o teto de 20 existe para isso.

Soma: cerca de 9 semanas até a primeira sombra com cliente, cerca de 12 a 13 até a decisão de chamar o vigia. A fase 0 e a fase 2 dependem da loja e de advogado; escorregam sem que o código escorregue.

---

## 10. Riscos

| Risco | O que acontece | Mitigação |
|---|---|---|
| A câmera do mercado não vê a mão | O classificador treina em ruído e a sombra dispara à toa, ou não dispara nunca | Fase 0 recusa a câmera. Abstenção quando o punho falta. Não compensar com limiar mais baixo |
| O número do paper não se transfere | PoseLift/Shopformer/RetailS são outra loja, outro esqueleto, outro fps | A métrica é a encenação desta loja e a semana de sombra. Literatura só compara método |
| Alarme falso mata o piloto | O fundador silencia o Telegram, que é o R-1 com outro canal | Sombra, teto, debounce por track, HPRS, negativos de “celular no bolso” e “pôr na cesta” |
| AGPL | Um `.onnx` da Ultralytics entra “só para testar” e o produto fechado fica contaminado | Remover o spec `YOLOV8`, recusar o nome do arquivo, pinagem com SHA no `modelo.sh`, COCO-17 e não WholeBody |
| RetailS sem licença clara | O peso da loja carrega dado que não se pode usar | Fica de fora até o `LICENSE` permitir uso comercial |
| Shopformer como detector | Anomalia vira alerta para qualquer gesto raro | Só baseline offline |
| CPU ou Orin no escuro | A §10 já mediu ~1 fps com tracking em CPU | Piloto na RTX. Orin depois, com TensorRT, 2 câmeras. `onnxruntime` de CPU não entra na imagem do box |
| Senha do DVR no log | O log do agente vaza a credencial da câmera | `redige_credencial` no supervisor, no probe e no stderr |
| Clipe eterno, e clipe no Telegram | Dado pessoal de cliente sem prazo | Expurgo 48 h no bucket, apagar a mensagem, sem esqueleto persistido, sem embedding facial. Aviso e base legal com advogado |
| Troca de ID no corredor cheio | A janela mistura duas pessoas e o alerta mostra a pessoa errada | Janela curta, idade mínima do track, recalibrar IoU a 8 fps com `--dump-tracks`, abstenção |
| Escopo de fundador solo | Dashboard, Jetson, Shopformer e PDV ao mesmo tempo | Este plano é o escopo. O resto da seção 8 permanece adiado |
| Atualização remota quebra a loja | R-7 | Digest pinado, uma loja, volta ao digest anterior se o heartbeat não subir |
| Token de desenvolvimento na loja | `seed.ts` publica senha e token conhecidos | Não rodar o seed no banco da loja. `register` com bootstrap de uso único |

---

## 11. Ordem de trabalho da primeira quinzena

1. Fase 0 no corredor, com o agente atual e `--dump-tracks`, ainda sem pose: confirmar que o substream dá para medir a altura da pessoa.
2. `redige_credencial` e a retirada do `YOLOV8`, com teste, enquanto a loja responde. São mudanças pequenas, já localizadas, e tiram risco que não depende do modelo novo.
3. Só então o export do RTMPose-s e a medição na RTX.

O gesto não começa pelo classificador. Começa pela câmera em que um humano, olhando o vídeo do próprio pipe, consegue ver a mão ir da prateleira ao bolso.

<!-- FIM DO PLANO HISTORICO -->
