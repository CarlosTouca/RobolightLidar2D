// Joystick (Gamepad API, ex.: PlayStation) -> rover simulado. Analógico esquerdo ou D-pad:
// frente/trás + giro viram a velocidade de cada lado (tração diferencial), enviada só quando muda.
import { api } from './api.js';

const DEADZONE = 0.15;
const clamp = (v) => Math.min(1, Math.max(-1, v));
const btn = (pad, i) => (pad.buttons[i] && pad.buttons[i].pressed ? 1 : 0);

export function initGamepad() {
  let sent = '0,0';
  let inflight = false;
  let want = null;   // último comando desejado enquanto há um pedido em curso (mantém a ordem)

  async function send(left, right) {
    if (inflight) { want = [left, right]; return; }
    inflight = true;
    try { await api.drive(left, right); } catch { /* sem conexão com o servidor/fonte: ignora */ }
    inflight = false;
    if (want) { const [l, r] = want; want = null; send(l, r); }
  }

  setInterval(() => {
    const pad = Array.from(navigator.getGamepads ? navigator.getGamepads() : []).find((p) => p && p.connected);
    let fwd = 0, turn = 0;
    if (pad) {
      const [ax, ay] = [pad.axes[0] || 0, pad.axes[1] || 0];
      if (Math.hypot(ax, ay) > DEADZONE) { fwd = -ay; turn = ax; }
      else { fwd = btn(pad, 12) - btn(pad, 13); turn = btn(pad, 15) - btn(pad, 14); }
      if (btn(pad, 0) || btn(pad, 1)) fwd = turn = 0;   // X ou O: parar
    }
    const left = Math.round(clamp(fwd + turn) * 100) / 100;
    const right = Math.round(clamp(fwd - turn) * 100) / 100;
    const key = `${left},${right}`;
    if (key !== sent) { sent = key; send(left, right); }   // sem controle: vira 0,0 e para
  }, 50);
}
