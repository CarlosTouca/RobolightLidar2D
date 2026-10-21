// Cliente WebSocket + desenho. Protocolo (ver backend/api.py):
//   servidor -> {"type":"scan","seq":N,"t":s,"x":[mm],"y":[mm],"q":[0..63]}
//   cliente  -> String(seq) depois de DESENHAR (ack). O servidor só envia a próxima
//   varredura depois do ack (ou de 2 s sem ack), então o atraso nunca se acumula.
import { api } from './api.js';
import { createPlot } from './plot.js';
import { initPanel } from './panel.js';
import { initGamepad } from './gamepad.js';

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

// Alcance: ?range= na URL > padrão do servidor (6 m) > 6 m.
const cfg = await api.config().catch(() => ({ default_range_m: 6 }));
const rangeM = clamp(
  parseFloat(new URLSearchParams(location.search).get('range')) || cfg.default_range_m || 6, 1, 12);
$('range').value = rangeM;
const plot = createPlot($('plot'), rangeM);

const RENDER_STUCK_MS = 1000; // desenho "pendurado" (aba ficou oculta no meio) é abandonado

let ws = null;
let retryMs = 500;
let retryTimer = null;

let pending = null;      // última varredura recebida e ainda não desenhada {msg, sock}
let renderId = 0;        // identifica o desenho em curso (ignora finais de desenhos abandonados)
let busy = false;
let busySince = 0;
let lastFrame = null;    // último quadro desenhado (para redesenhar ao trocar a cor)
let serverState = 'disconnected';   // estado informado pelo painel (GET /api/status)
let clearAfter = false;   // clearView() chamado com um desenho em curso
const drawTimes = [];    // instantes dos desenhos recentes (para o FPS)

// Depuração/verificação manual: window.__lidar
const dbg = (window.__lidar = { drawn: 0, received: 0, reconnects: 0, lastSeq: 0, hiddenHeld: 0 });

function setStatus(state, text) {
  $('status').dataset.state = state;
  $('status-text').textContent = text;
}

// ---- WebSocket ------------------------------------------------------------------
function connect() {
  clearTimeout(retryTimer);
  setStatus('connecting', 'conectando…');
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const sock = new WebSocket(`${proto}://${location.host}/ws`);
  ws = sock;

  sock.onopen = () => {
    retryMs = 500;
    setStatus('open', 'conectado');
  };
  sock.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === 'pose') { plot.setPose(msg.pose); return; }   // posição do rover, sem ack
    if (msg.type !== 'scan') return;
    dbg.received++;
    if (serverState !== 'scanning') {   // chegou após parar/desconectar: não desenhar
      ack(sock, msg.seq);
      return;
    }
    pending = { msg, sock };   // só a mais recente interessa
    pump();
  };
  sock.onerror = () => sock.close();
  sock.onclose = () => {
    if (ws !== sock) return;
    ws = null;
    pending = null;
    busy = false;               // um desenho de socket antigo não deve travar o novo
    renderId++;
    dbg.reconnects++;
    setStatus('closed', `desconectado — reconectando em ${(retryMs / 1000).toFixed(1)} s`);
    retryTimer = setTimeout(connect, retryMs);
    retryMs = Math.min(retryMs * 2, 3000);
  };
}

function ack(sock, seq) {
  if (sock === ws && sock.readyState === WebSocket.OPEN) sock.send(String(seq));
}

// ---- Desenho --------------------------------------------------------------------
async function pump() {
  if (busy || !pending) return;
  if (document.hidden) { dbg.hiddenHeld++; return; }  // oculta: segura sem desenhar nem dar ack
  const { msg, sock } = pending;
  pending = null;
  busy = true;
  busySince = performance.now();
  const id = ++renderId;
  try {
    await plot.update(msg, $('color-q').checked);
    showMapCount();
    lastFrame = msg;
    drawTimes.push(performance.now());
    dbg.drawn++;
    dbg.lastSeq = msg.seq;
    $('points').textContent = msg.x.length;
  } catch (err) {
    console.error('falha ao desenhar', err);
  } finally {
    ack(sock, msg.seq);           // sempre, mesmo com erro: não deixa o fluxo travar
    if (id === renderId) {
      busy = false;
      if (clearAfter) { clearAfter = false; clearView(); } else pump();
    }
  }
}

// Aba oculta: nada é desenhado nem confirmado (o servidor espera, e libera uma por 2 s).
// Ao voltar, desenha a mais recente retida e o fluxo continua.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (busy && performance.now() - busySince > RENDER_STUCK_MS) {
    busy = false;                 // desenho interrompido pela aba oculta: abandona
    renderId++;
  }
  pump();
});

$('color-q').addEventListener('change', () => {
  if (lastFrame && !busy) plot.update(lastFrame, $('color-q').checked);
});

// ---- Contadores -------------------------------------------------------------------
setInterval(() => {
  const now = performance.now();
  while (drawTimes.length && now - drawTimes[0] > 1000) drawTimes.shift();
  $('fps').textContent = drawTimes.length.toFixed(1);
}, 250);

// Varredura parada/desconectada: não deixar pontos antigos na tela (podem enganar).
function clearView() {
  pending = null;
  lastFrame = null;
  drawTimes.length = 0;
  $('points').textContent = '0';
  if (busy) clearAfter = true;   // o desenho em curso termina e então a tela é limpa
  else plot.update(null, true);
}

initPanel({
  onRange: (m) => plot.setRange(m),
  onState: (s) => {
    if (s === 'disconnected' && serverState !== 'disconnected') { plot.clearMap(); showMapCount(); }   // nova sessão = outras coordenadas
    serverState = s;
  },
  onStopped: clearView,
});

initGamepad(plot);

// ---- Mapa guardado: guardar/limpar/salvar --------------------------------------------------
function showMapCount() {
  const n = plot.mapSize();
  $('map-count').textContent = `${n.toLocaleString('pt-BR')} ${n === 1 ? 'ponto guardado' : 'pontos guardados'}`;
  $('map-clear').disabled = $('map-save').disabled = n === 0;
}
$('map-keep').addEventListener('change', () => plot.setKeep($('map-keep').checked));
$('map-clear').addEventListener('click', () => { plot.clearMap(); showMapCount(); });
$('map-save').addEventListener('click', () => {
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, (c) => (c === 'T' ? '_' : ''));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([plot.mapCsv()], { type: 'text/csv;charset=utf-8' }));
  a.download = `mapa_${stamp}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

plot.update(null, true); // enquadramento inicial (eixos, anéis, sensor) via Plotly.react
connect();
