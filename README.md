# Lidar 2D — visualizador para o Slamtec RPLIDAR C1

Visualizador 2D em tempo real (vista de cima, sensor na origem) para pesquisa: nuvem de pontos
ao vivo de um RPLIDAR C1, ou de um simulador intercambiável. Backend FastAPI + WebSocket,
frontend HTML + Plotly.js (Scattergl).

## Estado do projeto

| Parte | Estado |
|---|---|
| Pipeline (filtro q=0/d=0, polar → mm inteiros), simulador, hub 5 fps, ack, WebSocket | Validado com testes automáticos e no navegador |
| Frontend (Plotly.react, zoom/pan estáveis, anéis, cor por qualidade, painel, reconexão) | Validado no navegador embutido com o simulador |
| API de controle e máquina de estados | Validado por testes (TestClient e servidor real) |
| Robustez (cabo puxado, porta ocupada, health em erro, WS, desligamento) | Validado **só com falhas simuladas** (simulador e serial falsa) |
| Parser (`rplidar_protocol.py`) | Validado contra uma captura real do C1 (9/out/2026): descritor correto e 65.702 de 65.702 pacotes válidos (`tests/fixtures/`) |
| **Driver do C1 (`rplidar_c1.py`) de ponta a ponta** | **Roteiro abaixo ainda não registrado**: motor, falhas físicas e demais itens de "Itens não verificados" não têm registro de teste |

Nada que dependa do aparelho está marcado como concluído. Veja "Itens não verificados" abaixo.

## Convenção de ângulo e coordenadas

- **0° aponta para a frente do sensor**, que no gráfico é o eixo **+Y** (para cima).
- O ângulo cresce no **sentido horário**, visto de cima: 90° = +X (direita),
  180° = −Y (trás), 270° = −X (esquerda).
- Conversão: `x = d·sin(θ)`, `y = d·cos(θ)`.
- Distâncias em **milímetros inteiros**; o sensor fica em (0, 0).

Definida em `backend/models.py` e implementada em `backend/pipeline.py`. O datasheet do C1
(§Coordinate System, p.11) descreve o mesmo sentido (x à frente, ângulo horário).

## Ambiente (Windows, Python 3.12)

```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## Testes

```
.venv\Scripts\python -m pytest -v
```
Cerca de 1 minuto (vários testes usam tempo real). Os testes de fixtures reais do C1 rodam com as
capturas em `tests/fixtures/` (ficam *skipped* se a pasta não tiver `c1_*_scan|info|health.bin`).
Nenhum teste abre porta serial real. Dois testes dependem de tempo (`test_robustness`, `test_hub`)
e podem falhar de vez em quando numa máquina carregada.

## Rodar e usar

```
.venv\Scripts\python -m backend.main
```
Abra http://127.0.0.1:8000/ (bind em `127.0.0.1`; mude com `LIDAR_HOST` / `LIDAR_PORT`).
O servidor sobe **desconectado**: no painel escolha a porta (SIMULADOR sempre presente),
clique em *Conectar* e *Iniciar varredura*. O alcance dos eixos (padrão 6 m, `LIDAR_RANGE_M`)
é ajustável no painel ou por `?range=8` na URL. Ctrl+C encerra: para a varredura, fecha a fonte
e a porta é liberada.

API de controle (JSON): `GET /api/config|ports|status`, `POST /api/connect {"port": ...}`,
`/api/disconnect`, `/api/scan/start`, `/api/scan/stop`. Transição inválida -> 409, falha da fonte
-> 502, ambos como `{"error": {"code", "message"}}` (ver `backend/api.py`).

**Rover simulado com joystick (estilo mouse).** Com a fonte SIMULADOR, `POST /api/steer {"x": -1..1, "y": -1..1,
"speed": 0..255}` pede uma direção **na tela** (x para a direita, y para cima): o rover gira até lá (a seta
acompanha) e anda. `frontend/gamepad.js` lê um controle (Gamepad API): analógico esquerdo (módulo = intensidade)
ou D-pad dão a direção, o **analógico direito gira o mapa** na tela (R3 volta a 0°; "cima" continua sendo cima
na tela girada), X/O param enquanto apertados e L1/R1 mudam a velocidade; o comando é repetido a cada 200 ms
e, sem comandos por 0,8 s, o rover para. Não é a lógica de tração do rover de verdade (letras F T E D
do firmware, onde esquerda/direita giram no lugar): é só uma forma intuitiva de explorar o mapa.

Com o simulador, cada quadro traz `pose` (`[x mm, y mm, rumo°]`) e o gráfico mostra o **mapa fixo**: a sala
fica parada e a seta do sensor anda e gira. Com o sensor real não há `pose` e o sensor fica na origem, como
antes. O simulador é uma **casa** de 10 x 8 m (`house_scene()`: sala, cozinha, corredor, dois quartos e banheiro, com
portas e móveis); o rover é um disco de 25 cm de raio que paredes e móveis bloqueiam. O driver real ignora
`/api/steer`. A posição do rover também sai a ~30/s por um canal à parte (`{"type": "pose"}`, sem ack, só a mais nova; `POSE_FPS`),
e a seta e a rotação do mapa deslizam entre as mensagens (`frontend/plot.js`); a seta gira com velocidade
proporcional ao erro (suave nos ajustes finos, no máximo 200°/s).

## Arquitetura e regras de desenvolvimento

Escopo: nuvem de pontos 2D ao vivo, vista de cima, sensor na origem, mais o painel de controle.
**Fora do escopo, sem pedido explícito:** SLAM, grade de ocupação, navegação, estatísticas de precisão,
modos de varredura além do Standard e controle de velocidade do motor.

```
Fonte (thread) → pipeline (filtra q=0/d=0, polar→x,y,q) → Hub (1 quadro) → 10 Hz → WebSocket → Plotly
```
- `backend/sources/`: contrato `LidarSource` (`base.py`), `simulator.py`, `rplidar_c1.py` (+ `rplidar_protocol.py`,
  parser puro) e `factory.py` (porta `SIMULADOR` → simulador, `COMx` → driver). **Só `sources/` importa pyserial.**
- `backend/session.py` (estados `disconnected → connected → scanning`), `backend/hub.py` (último quadro e difusão),
  `backend/api.py` (REST + `/ws`), `backend/main.py`, `frontend/` (HTML/JS sem build).

Regras que não devem ser quebradas:
1. No máximo **10 quadros/s** (`MAX_FPS`, o ritmo do C1); o excesso é descartado, nunca enfileirado.
2. **Fila de tamanho 1**: o hub guarda só o último quadro e cada cliente tem um único slot.
3. **Ack obrigatório**: o cliente envia `String(seq)` depois de DESENHAR (dentro de um `finally`); o servidor
   mantém no máximo 1 varredura sem ack em trânsito e libera outra após 2 s sem ack.
4. **`Plotly.react` sempre, nunca `newPlot`** (um teste varre o código) e **`uirevision` constante**: só
   `setRange()` o muda, para o zoom/pan do usuário não resetar.
5. Envio compacto (`x[]`, `y[]` em mm inteiros, `q[]`); pontos com qualidade 0 ou distância 0 são
   descartados no backend (`pipeline.py`).
6. Varredura parada ou desconectada: `hub.clear()` e a tela é limpa; ninguém recebe quadro "congelado".
7. `Session.status()` não pode pegar o lock (o painel consulta a cada 1 s).
8. A conversão polar → cartesiano existe só em `pipeline.py` (e no `simulator.py` ao gerar); não a reimplemente.
9. **Nunca abra porta serial real em testes** nem ao experimentar, e evite as COM3–COM8 deste PC (Bluetooth).
   Use `FakeSerial` (`tests/fake_serial.py`) e o simulador.

Armadilhas já encontradas: o uvicorn re-emite o sinal ao desligar (código de saída 3 no Windows é normal);
`receive_text()` + `int(text)` quebra com binário ou 5000 dígitos (use o handler atual de `/ws`);
`tracemalloc` deixa a codificação JSON lenta (testes de memória usam 15 mil pontos); os bits de
verificação do modo Standard são fracos (o decodificador exige 3 pacotes válidos seguidos).

Plotly vem de `frontend/vendor/plotly-gl2d.min.js` (v2.35.2, MIT, baixado de cdn.plot.ly);
a página não usa CDN.

## Inspecionar o simulador

```
.venv\Scripts\python scripts\preview_simulator.py          # salva scripts\preview_simulator.png
.venv\Scripts\python scripts\preview_simulator.py --show   # abre janela (use --scans 5 para sobrepor)
```

## Robustez (etapa 7, só simulador e serial falsa)

Coberto por `tests/test_robustness.py` e confirmado removendo cada correção para ver o teste falhar:

- **Cabo puxado durante a varredura** (`Simulator.inject_fault("disconnect")`): estado volta a
  *conectado*, o erro aparece no painel, nenhum quadro "congelado" fica no hub, a thread termina;
  reiniciar dá erro claro; desconectar e conectar de novo restabelece o fluxo.
- **Health em erro**: a saúde (com código) aparece no painel; iniciar a varredura falha com
  "Protection Stop" até a saúde voltar ao normal.
- **Porta ocupada** e **dispositivo mudo**: mensagens claras ("ocupada…", "sem resposta…"); a sessão
  continua utilizável e uma nova tentativa funciona.
- **Leitura travada**: se a thread não obedece ao pedido de parada, o erro é informado, uma segunda
  thread não é criada (`reader_busy`) e a thread antiga nunca publica depois do disconnect.
- **WebSocket**: lixo enviado pelo cliente (binário, números gigantes, dígitos não-ASCII) é ignorado;
  30 quedas bruscas não vazam canais; 25 clientes simultâneos, um deles sem dar ack, não afetam os
  demais; o painel mostra "Servidor indisponível" e se ressincroniza quando o servidor volta.
- **Concorrência**: 8 threads disparando conectar/varrer/parar/desconectar ao acaso por 3 s não
  geram exceções inesperadas nem deixam thread de leitura viva.
- **Encerramento**: processo real recebe Ctrl+C com cliente conectado e varredura ativa e sai em
  poucos segundos, com `Application shutdown complete`, sem traceback e liberando a porta. (No Windows
  o código de saída é 3: o uvicorn re-emite o sinal depois de desligar.)

## Roteiro de captura com o C1 (para fechar a etapa 6)

Pré-requisitos: sensor alimentado em 5 V pelo adaptador USB do kit; driver CP210x instalado
(o manual do usuário, §Install Driver for the USB cable, indica o chip CP2102).

1. Conecte o C1 e liste as portas. Procure uma "Silicon Labs CP210x USB to UART Bridge … [USB 10C4:EA60]".
   **Não use as portas "Serial Padrão por link Bluetooth"** (COM3 a COM8 neste PC): abri-las pode iniciar uma
   conexão Bluetooth.
   ```
   .venv\Scripts\python scripts\capture_bytes.py --list
   ```
2. (Opcional, sem ligar o motor) só informações do dispositivo:
   ```
   .venv\Scripts\python scripts\capture_bytes.py --port COM9 --no-scan
   ```
3. Captura completa (liga o motor por ~15 s; aguarde 2 min de aquecimento se quiser a melhor precisão):
   ```
   .venv\Scripts\python scripts\capture_bytes.py --port COM9 --seconds 10
   ```
   Saída em `captures/`: `c1_*_info.bin`, `c1_*_health.bin`, `c1_*_scan.bin`, `c1_*_meta.json`.
   Anote no que o resumo mostrar: `descritor_hex` deve ser `a5 5a 05 00 00 40 81` e `fracao_valida`
   próxima de 1.
4. Copie os três `.bin` para `tests/fixtures/` e rode:
   ```
   .venv\Scripts\python -m pytest tests\test_rplidar_fixtures.py -v
   ```
   Os testes deixam de ser *skipped*. Se algum falhar, envie o `_meta.json` e o `.bin`.
5. Valide no painel: escolha a porta do C1, *Conectar* (modelo/firmware/série/saúde reais), *Iniciar
   varredura* e confira: o motor gira e para ao parar; a sala aparece com a orientação certa
   (frente do sensor = +Y, horário); "Pacotes inválidos" fica ≈ 0; ~500 pontos por varredura.
6. Teste as falhas **de verdade**: puxe o cabo USB durante a varredura (erro "cabo desconectado" no painel,
   sem travar); abra a porta em outro programa antes de conectar ("ocupada").

Só depois de 4 e 5 a etapa 6 pode ser marcada como concluída.

## Validação do protocolo contra os documentos da Slamtec

| Item | Fonte (seção / página) |
|---|---|
| 460800 baud, 8N1, TTL 3,3 V | Datasheet C1 rev.1.0, §Data Communication Interface, p.14, Fig. 2-8 |
| Pinos: VCC, TX, RX, GND (sem pino de motor) | Datasheet C1, Fig. 2-6, p.12 |
| C1 compatível com protocolo da série S e standard da série A | Datasheet C1, §Protocol Compatibility, p.7 |
| Ângulo cresce no sentido horário, x à frente | Datasheet C1, §Coordinate System, p.11 |
| Formato do pedido (A5, cmd, [tam, payload, checksum XOR]) | Protocolo v2.2, §2.3, p.6-7, Fig. 2-4 |
| STOP `A5 25` (esperar ≥ 1 ms), RESET `A5 40` (≥ 2 ms) | Protocolo v2.2, §4.2 p.14, §4.3 p.15 |
| SCAN `A5 20`, descritor `A5 5A 05 00 00 40 81`, 5 bytes/medida | Protocolo v2.2, §4.4, p.15-16, Fig. 4-4 |
| Campos: S, S̅=!S, C=1, quality, angle_q6/64, distance_q2/4 | Protocolo v2.2, p.16-17, Fig. 4-4/4-5 |
| Descritor: A5 5A + 30 bits de tamanho + 2 bits de modo + tipo | Protocolo v2.2, §2.4, p.8-9, Figs. 2-7/2-8 |
| GET_INFO `A5 50`, descritor `A5 5A 14 00 00 00 04`, 20 bytes | Protocolo v2.2, p.34-35, Figs. 4-25/4-26 |
| GET_HEALTH `A5 52`, descritor `A5 5A 03 00 00 00 06`, 3 bytes | Protocolo v2.2, p.36, Figs. 4-28/4-29 |
| Fluxo recomendado: GET_HEALTH → (RESET se Protection Stop) → SCAN | Protocolo v2.2, §5.1, p.45, Fig. 5-1 |

**Motor.** Datasheet C1, §Scanner Motor Control (p.14): "the motor can't start or stop alone, and its
working state depends on the laser scan command". Portanto o driver **não usa DTR/PWM**: SCAN liga
o motor e STOP o desliga. O manual do usuário do C1 (§Pin Definition, p.12) menciona um sinal
"MOTOCTL", mas a tabela de pinos do mesmo manual não tem esse pino; tratei como texto herdado de
outro modelo. O comando de velocidade `A8` do manual v2.2 (§4.11, p.44) é só da série S1 e **não é usado**.

**Limite do modo Standard.** Só há 3 bits de verificação por pacote (S/S̅ e C, mais a faixa do ângulo):
um alinhamento errado passa em ~1 de cada 6 vezes. Por isso o decodificador só reaceita o
alinhamento após 3 pacotes válidos seguidos; ainda assim um pacote espúrio isolado pode passar.

## Itens não verificados (dependem do aparelho ou de documento inacessível)

Nada abaixo deve ser tratado como confirmado:

1. **O driver inteiro com o C1 real**: handshake STOP → GET_INFO → GET_HEALTH, SCAN, parada, reconexão.
2. **O motor**: que SCAN o liga e STOP o desliga, sem sinal extra (a contradição "MOTOCTL" do manual do
   usuário só se resolve no aparelho).
3. **O formato dos bytes reais** contra o manual v2.2: descritor, pacote de 5 bytes, bits S/S̅/C.
4. **O manual v2.8 (S&C series)**, que lista o C1, respondeu HTTP 403 e **não foi lido**. O que dependeria
   dele: tabela do ID de modelo (o painel mostra "RPLIDAR (ID 0x..)", sem inventar o nome), comando de
   velocidade do motor, e quaisquer particularidades do C1 no modo Standard.
5. **Escala da qualidade no C1**: assumida 0..63 (6 bits). O colorbar e o simulador usam esse limite.
6. **A taxa real do C1 no modo Standard**: o datasheet diz 5 kHz / 10 Hz / 0,72° (~500 pontos por volta),
   mas o manual v2.2 avisa que modelos de ≥ 4 kHz podem reduzir a taxa no SCAN (p.15); o modo de maior
   densidade (EXPRESS_SCAN) não está implementado.
7. **As mensagens de erro de porta ocupada/inexistente** foram escritas para o texto das exceções do
   pyserial no Windows; foram testadas com exceções sintéticas semelhantes, não com portas reais.
8. **O tempo de espera de 0,5 s após RESET** é uma escolha minha (o manual exige só ≥ 2 ms).
9. **O comportamento do navegador com o painel Browser oculto**: as verificações visuais foram feitas
   no navegador embutido; em algumas execuções ele ficou oculto e só a lógica (não o desenho) foi observada.
10. **Falhas físicas reais** (cabo puxado, queda de energia, motor travado): só simuladas.
