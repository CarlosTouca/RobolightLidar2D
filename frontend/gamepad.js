// Joystick (Gamepad API, ex.: PlayStation) -> rover simulado, estilo mouse: o analógico esquerdo ou o
// D-pad dão a direção NA TELA (cima = para cima no mapa); a seta gira até lá e o rover anda.
// O analógico DIREITO gira o mapa na tela (clicar nele, R3, volta a 0°); "cima" continua sendo cima na tela
// girada. X/O = parar (enquanto apertados); L1/R1 = velocidade -/+. Só envia ao mudar e repete a cada 200 ms
// em movimento: o servidor para o rover sozinho se os comandos deixarem de chegar.
import { api } from './api.js';

const DEADZONE = 0.3;
const ROT_DEADZONE = 0.2;
const ROT_DEG_S = 90;       // giro do mapa com o analógico direito todo para o lado
const HEARTBEAT_MS = 200;
const SPEED_STEP = 25;
const pressed = (pad, i) => !!(pad.buttons[i] && (pad.buttons[i].pressed || pad.buttons[i].value > 0.65));
const round2 = (v) => Math.round(v * 100) / 100;

/** `plot`: gráfico (setRotation gira o mapa). */
export function initGamepad(plot) {
  let speed = 150;            // mesmo padrão do slider da página do rover
  let sent = '0,0';
  let sentSpeed = speed;
  let sentAt = 0;
  let prevL1 = false, prevR1 = false, prevR3 = false;
  let rot = 0;                // rotação do mapa na tela (graus, horário)
  let rotAt = performance.now();
  const padInfo = document.getElementById('pad-info'), padAxes = document.getElementById('pad-axes');
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
      if (Math.hypot(ax, ay) > DEADZONE) { x = ax; y = ay; }       // analógico: o módulo vira a intensidade
      else {
        x = pressed(pad, 15) - pressed(pad, 14);                    // D-pad: direita - esquerda
        y = pressed(pad, 12) - pressed(pad, 13);                    //        cima - baixo
        const m = Math.hypot(x, y);
        if (m > 1) { x /= m; y /= m; }                              // diagonal não anda mais rápido
      }
      if (pressed(pad, 0) || pressed(pad, 1)) x = y = 0;            // X ou O: parada de emergência

      // Direção pedida na tela -> direção no mundo (o mapa está girado em `rot`).
      const c = Math.cos(rot * Math.PI / 180), s = Math.sin(rot * Math.PI / 180);
      [x, y] = [x * c - y * s, x * s + y * c];

      const rx = pad.axes[2] || 0;                                   // analógico direito: gira o mapa
      if (Math.abs(rx) > ROT_DEADZONE) rot = (rot + rx * ROT_DEG_S * (now - rotAt) / 1000 + 360) % 360;
      const r3 = pressed(pad, 11);
      if (r3 && !prevR3) rot = 0;
      prevR3 = r3;
      plot.setRotation(rot);

      const l1 = pressed(pad, 4), r1 = pressed(pad, 5);
      speed = Math.max(0, Math.min(255, speed + ((r1 && !prevR1) - (l1 && !prevL1)) * SPEED_STEP));
      prevL1 = l1; prevR1 = r1;
    }
    rotAt = now;
    if (padInfo) {                                   // diagnóstico: mostra o que o navegador enxerga do controle
      padInfo.textContent = pad ? `${pad.mapping || 'sem mapeamento'} · giro ${Math.round(rot)}°` : 'não detectado (aperte um botão)';
      padAxes.textContent = pad ? pad.axes.map((a) => a.toFixed(1)).join('  ') : '—';
    }
    x = round2(x); y = round2(y);
    const key = `${x},${y}`;
    if (key !== sent || speed !== sentSpeed || (key !== '0,0' && now - sentAt > HEARTBEAT_MS)) {
      sent = key; sentSpeed = speed; sentAt = now;
      send(x, y, speed);
    }
  }, 50);
}
