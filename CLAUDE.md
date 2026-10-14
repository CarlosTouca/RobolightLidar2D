# CLAUDE.md — Lidar 2D

Visualizador 2D em tempo real para o LiDAR Slamtec RPLIDAR C1, para pesquisa. Idioma do projeto
(código, comentários, mensagens de erro, UI): português do Brasil. Ambiente: Windows, Python 3.12,
`.venv` na raiz. Leia o README para uso e roteiro de captura.

## Escopo

- **Dentro:** nuvem de pontos 2D ao vivo, vista de cima, sensor na origem, para um humano interpretar;
  painel de controle (porta, conectar/desconectar, iniciar/parar, status, alcance, cor por qualidade).
- **Fora (não implementar sem pedido):** SLAM, grade de ocupação, navegação, estatísticas de precisão,
  modos de varredura além do Standard (EXPRESS_SCAN etc.), controle de velocidade do motor.

## Convenção de ângulo (vale em TODO o projeto)

- **0° = frente do sensor = +Y (para cima); o ângulo cresce no sentido horário** (90° = +X).
- `x = d·sin(θ)`, `y = d·cos(θ)`; distâncias em **mm inteiros**; sensor em (0, 0).
- Documentada em `backend/models.py`; implementada só em `backend/pipeline.py` (e `simulator.py` ao gerar).
  Não reimplemente a conversão em outro lugar.

## Arquitetura (resumo)

```
Fonte (thread) → pipeline (filtra q=0/d=0, polar→x,y,q) → Hub (1 quadro) → 5 Hz → WebSocket → Plotly
```
- `backend/sources/`: contrato `LidarSource` (`base.py`); `simulator.py`; `rplidar_c1.py` (+ `rplidar_protocol.py`,
  parser puro); `factory.py` escolhe pela porta (`SIMULADOR` → simulador, `COMx` → driver). **Só `sources/`
  importa pyserial**; o resto do sistema não sabe qual fonte está em uso.
- `backend/session.py`: estados `disconnected → connected → scanning`, thread de leitura, erros.
- `backend/hub.py`: último quadro + difusão limitada + controle de fluxo por cliente.
- `backend/api.py` (REST + `/ws`), `backend/main.py` (app, ciclo de vida), `frontend/` (HTML/JS, sem build).

## Regras de desempenho e fluxo (não quebre)

1. **5 quadros/s no máximo** (`MAX_FPS`), medido entre ticks do broadcaster (espaçamento mínimo de 200 ms,
   sem rajadas). A fonte pode produzir 10 Hz; o excesso é descartado, nunca enfileirado.
2. **Fila de tamanho 1**: o hub guarda só o último quadro; cada cliente tem um slot que a mensagem nova
   sobrescreve. Nenhuma estrutura cresce com um cliente lento.
3. **Ack obrigatório**: o cliente envia `String(seq)` depois de DESENHAR (dentro de um `finally`); o servidor
   mantém no máximo 1 varredura sem ack em trânsito e libera outra após `ACK_TIMEOUT_S` (2 s) sem ack.
   Sem isso, os buffers TCP enfileirariam quadros velhos. O estado em trânsito é por conexão (zera ao
   reconectar). Aba oculta: o frontend retém sem desenhar nem confirmar e retoma ao voltar.
4. **`Plotly.react` sempre, nunca `newPlot`** (nem em comentário: um teste varre o código).
5. **`uirevision` constante entre quadros** (zoom/pan do usuário não pode resetar). Só `setRange()` o muda
   (ação explícita do usuário). O layout é construído uma vez; cada quadro só troca os dados.
6. Envio **compacto**: arrays `x[]`, `y[]` (mm inteiros) e `q[]`; nunca objetos por ponto.
7. Descartar pontos com qualidade 0 ou distância 0 **no backend** (`pipeline.py`).
8. Varredura parada/desconectada: `hub.clear()` e a tela é limpa; ninguém recebe quadro "congelado".
9. `Session.status()` não pode pegar o lock (o painel consulta a cada 1 s e não deve travar durante um
   `connect()` lento).

## Testes e verificação

- `.venv\Scripts\python -m pytest -q` (~1 min). Esperado: tudo verde, **3 skipped** (fixtures reais do C1).
- **Nunca abra porta serial real em testes** nem ao experimentar: as COM3–COM8 deste PC são Bluetooth e abrir
  pode iniciar conexão Bluetooth. Use `FakeSerial` (`tests/fake_serial.py`) e o simulador.
- Falhas físicas se simulam com `Simulator.inject_fault("disconnect")`, `Simulator(health=...)` e `FakeSerial`.
- Ao corrigir robustez, confirme que o teste novo FALHA sem a correção (mutação), como feito na etapa 7.
- Verificação visual: servidor com `python -m backend.main`, página em http://127.0.0.1:8000/ (o navegador
  embutido às vezes fica oculto; então só a lógica é observável, não o desenho).

## O que está validado (apenas com simulador / serial falsa / navegador)

Pipeline e convenção de ângulo; simulador (geometria, ruído, perdas); hub (limite de 5/s, `seq` crescente,
cliente lento sem acúmulo, ack e timeout, reconexão zera o estado); WebSocket com clientes defeituosos e quedas
bruscas; API e máquina de estados (409/502 com mensagens claras); frontend (Plotly.react, zoom estável, anéis,
cor por qualidade, painel, reconexão, ressincronização após reiniciar o servidor); encerramento limpo do
processo; parser do protocolo e driver **contra bytes sintéticos escritos a partir do manual v2.2**.

## PENDENTE DE VALIDAÇÃO COM O HARDWARE (não marcar como concluído)

- Driver `rplidar_c1.py` e parser `rplidar_protocol.py` com o C1 real; fixtures reais em `tests/fixtures/`.
- Motor: SCAN liga / STOP desliga, sem DTR/PWM (datasheet p.14; o manual do usuário cita "MOTOCTL" sem pino).
- Manual S&C v2.8 do C1 **não foi lido** (HTTP 403): ID de modelo, comando de velocidade e particularidades do C1.
- Escala de qualidade 0..63; taxa real no modo Standard; mensagens de erro de porta com portas reais;
  espera de 0,5 s após RESET (escolha minha); falhas físicas reais (cabo puxado, queda de energia).
- Roteiro completo para fechar: README, "Roteiro de captura com o C1" (`scripts/capture_bytes.py`).

Regra: se algo depende do aparelho, **diga que não foi verificado**; não deduza formatos nem comportamentos
do sensor além do que os documentos Slamtec lidos dizem (cite seção/página).

## Armadilhas já encontradas

- O uvicorn re-emite o sinal depois de desligar: código de saída 3 no Windows com Ctrl+Break é normal.
- `receive_text()` + `int(text)` quebra com binário, dígitos não-ASCII ou 5000 dígitos: use o handler atual de `/ws`.
- `tracemalloc` deixa a codificação JSON de quadros grandes lenta; testes de memória usam quadros de 15 mil pontos.
- Bits de verificação do modo Standard são fracos (~1 em 6 alinhamentos errados passa): o decodificador
  exige 3 pacotes válidos seguidos para travar o alinhamento.
