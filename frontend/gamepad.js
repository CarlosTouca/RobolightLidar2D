// Joystick (Gamepad API, ex.: PlayStation) -> rover simulado. Mesma lógica da página do rover:
// D-pad ou analógico esquerdo viram as letras do firmware (F T E D G I H J P); X/O = parar
// (enquanto apertados); L1/R1 = velocidade -/+. Só envia ao mudar e repete a cada 200 ms em movimento:
// o servidor para o rover sozinho se os comandos deixarem de chegar.
import { api } from './api.js';

const DEADZONE = 0.3;
const HEARTBEAT_MS = 200;
const SPEED_STEP = 25;
const pressed = (pad, i) => !!(pad.buttons[i] && (pad.buttons[i].pressed || pad.buttons[i].value > 0.65));

export function initGamepad() {
  let speed = 150;            // mesmo padrão do slider da página do rover
  let sent = 'P';
  let sentSpeed = speed;
  let sentAt = 0;
  let prevL1 = false, prevR1 = false;
  let inflight = false;
  let want = null;            // último comando desejado enquanto há um pedido em curso (mantém a ordem)

  async function send(cmd, spd) {
    if (inflight) { want = [cmd, spd]; return; }
    inflight = true;
    try { await api.drive(cmd, spd); } catch { /* sem conexão com o servidor/fonte: ignora */ }
    inflight = false;
    if (want) { const [c, s] = want; want = null; send(c, s); }
  }

  setInterval(() => {
    const pad = Array.from(navigator.getGamepads ? navigator.getGamepads() : []).find((p) => p && p.connected);
    let cmd = 'P';
    if (pad) {
      const ax = pad.axes[0] || 0, ay = pad.axes[1] || 0;
      let f = pressed(pad, 12) || ay < -DEADZONE, t = pressed(pad, 13) || ay > DEADZONE;
      let e = pressed(pad, 14) || ax < -DEADZONE, d = pressed(pad, 15) || ax > DEADZONE;
      if (f && t) f = t = false;   // direções opostas se cancelam
      if (e && d) e = d = false;
      cmd = f ? (e ? 'G' : d ? 'I' : 'F') : t ? (e ? 'H' : d ? 'J' : 'T') : e ? 'E' : d ? 'D' : 'P';
      if (pressed(pad, 0) || pressed(pad, 1)) cmd = 'P';   // X ou O: parada de emergência

      const l1 = pressed(pad, 4), r1 = pressed(pad, 5);
      speed = Math.max(0, Math.min(255, speed + ((r1 && !prevR1) - (l1 && !prevL1)) * SPEED_STEP));
      prevL1 = l1; prevR1 = r1;
    }
    const now = performance.now();
    if (cmd !== sent || speed !== sentSpeed || (cmd !== 'P' && now - sentAt > HEARTBEAT_MS)) {
      sent = cmd; sentSpeed = speed; sentAt = now;
      send(cmd, speed);
    }
  }, 50);
}
