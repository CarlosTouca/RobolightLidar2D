// Joystick (Gamepad API, ex.: PlayStation) -> rover simulado, estilo mouse: o analógico esquerdo ou o
// D-pad dão a direção NA TELA (cima = para cima no mapa); o rover gira até lá e anda.
// O analógico DIREITO gira o mapa na tela (clicar nele, R3, volta a 0°); "cima" continua sendo cima na tela
// girada. X/O = parar (enquanto apertados); L1/R1 = velocidade -/+. Só envia ao mudar e repete a cada 200 ms
// em movimento: o servidor para o rover sozinho se os comandos deixarem de chegar.
import { api } from './api.js';

const DEADZONE = 0.2;
const ROT_DEADZONE = 0.25;
const ROT_START = 0.5;        // o giro só começa com o analógico direito bem empurrado: ruído não gira o mapa
const ROT_DEG_S = 90;       // giro do mapa com o analógico direito todo para o lado
const HEARTBEAT_MS = 200;
const SPEED_STEP = 25;
const pressed = (pad, i) => !!(pad.buttons[i] && (pad.buttons[i].pressed || pad.buttons[i].value > 0.65));
const round2 = (v) => Math.round(v * 100) / 100;

// Adaptadores USB genéricos de controle de PlayStation ("USB Joystick", sem mapeamento padrão) entregam o D-pad
// como um eixo "hat" (aqui o 9): -1 = cima e, a cada 2/7, no sentido horário; fora de [-1, 1] = solto.
const HAT_AXIS = 9;
const HAT_DIRS = [[0, 1], [1, 1], [1, 0], [1, -1], [0, -1], [-1, -1], [-1, 0], [-1, 1]];
const hat = (v) => (v >= -1.01 && v <= 1.01 ? HAT_DIRS[Math.round((v + 1) * 3.5) % 8] : [0, 0]);

/** `plot`: gráfico (setRotation gira o mapa). */
export function initGamepad(plot) {
  let speed = 150;            // mesmo padrão do slider da página do rover
  let sent = '0,0';
  let sentSpeed = speed;
  let sentAt = 0;
  let prevL1 = false, prevR1 = false, prevR3 = false;
  let rot = 0;                // rotação do mapa na tela (graus, horário)
  let rotAt = performance.now();
  let rotating = false;       // histerese do giro: começa acima de ROT_START, continua até abaixo de ROT_DEADZONE
  let inflight = false;
  let want = null;            // último comando desejado enquanto há um pedido em curso (mantém a ordem)

  async function send(x, y, spd) {
    if (inflight) { want = [x, y, spd]; return; }
    inflight = true;
    try { await api.steer(x, y, spd); } catch { /* sem conexão com o servidor/fonte: ignora */ }
    inflight = false;
    if (want) { const [a, b, s] = want; want = null; send(a, b, s); }
  }

  setInterval(() => {
    const pad = Array.from(navigator.getGamepads ? navigator.getGamepads() : []).find((p) => p && p.connected);
    let x = 0, y = 0;
    const now = performance.now();
    if (pad) {
      const ax = pad.axes[0] || 0, ay = -(pad.axes[1] || 0);       // eixo Y do controle: para cima é negativo
      const m = Math.hypot(ax, ay);
      if (m > DEADZONE) {                                          // analógico: a intensidade cresce a partir da zona morta
        const k = Math.min(1, (m - DEADZONE) / (1 - DEADZONE)) / m;
        x = ax * k; y = ay * k;
      }
      else {
        const [hx, hy] = pad.mapping === 'standard' ? [0, 0] : hat(pad.axes[HAT_AXIS]);
        x = pressed(pad, 15) - pressed(pad, 14) + hx;               // D-pad: direita - esquerda
        y = pressed(pad, 12) - pressed(pad, 13) + hy;               //        cima - baixo
        const m = Math.hypot(x, y);
        if (m > 1) { x /= m; y /= m; }                              // diagonal não anda mais rápido
      }
      if (pressed(pad, 0) || pressed(pad, 1)) x = y = 0;            // X ou O: parada de emergência

      // Direção pedida na tela -> direção no mundo (o mapa está girado em `rot`).
      const c = Math.cos(rot * Math.PI / 180), s = Math.sin(rot * Math.PI / 180);
      [x, y] = [x * c - y * s, x * s + y * c];

      const rx = pad.axes[2] || 0;                                   // analógico direito: gira o mapa
      rotating = Math.abs(rx) > (rotating ? ROT_DEADZONE : ROT_START);
      if (rotating) rot = (rot + rx * ROT_DEG_S * (now - rotAt) / 1000 + 360) % 360;
      const r3 = pressed(pad, 11);
      if (r3 && !prevR3) rot = 0;
      prevR3 = r3;
      plot.setRotation(rot);

      const l1 = pressed(pad, 4), r1 = pressed(pad, 5);
      speed = Math.max(0, Math.min(255, speed + ((r1 && !prevR1) - (l1 && !prevL1)) * SPEED_STEP));
      prevL1 = l1; prevR1 = r1;
    }
    rotAt = now;
    x = round2(x); y = round2(y);
    const key = `${x},${y}`;
    if (key !== sent || speed !== sentSpeed || (key !== '0,0' && now - sentAt > HEARTBEAT_MS)) {
      sent = key; sentSpeed = speed; sentAt = now;
      send(x, y, speed);
    }
  }, 50);
}
