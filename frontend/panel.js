// Painel de controle: porta, conectar/desconectar, iniciar/parar, status, alcance.
// O estado mostrado vem SEMPRE do servidor (GET /api/status a cada 1 s e após cada comando);
// os botões são habilitados só nas transições válidas.
import { api } from './api.js';

const $ = (id) => document.getElementById(id);
const STATE_LABEL = { disconnected: 'desconectado', connected: 'conectado', scanning: 'varrendo' };
const HEALTH_LABEL = { ok: 'OK', warning: 'aviso', error: 'erro' };
const POLL_MS = 1000;

/**
 * hooks.onRange(m)          — usuário aplicou novo alcance
 * hooks.onState(state)    — estado do servidor a cada atualização
 * hooks.onStopped()         — a varredura deixou de estar ativa (limpar o gráfico)
 */
export function initPanel(hooks) {
  let status = null;       // último /api/status
  let busy = false;        // comando em andamento
  let cmdError = '';       // erro do último comando (some no próximo comando bem-sucedido)
  let offline = false;     // servidor inacessível

  function showError() {
    const el = $('error');
    const text = cmdError || (offline ? 'Servidor indisponível.' : '') || (status && status.error) || '';
    el.textContent = text;
    el.hidden = !text;
  }

  function render() {
    const state = status ? status.state : 'disconnected';
    const port = $('port');
    $('btn-connect').disabled = busy || offline || state !== 'disconnected' || !port.value;
    $('btn-disconnect').disabled = busy || offline || state === 'disconnected';
    $('btn-start').disabled = busy || offline || state !== 'connected';
    $('btn-stop').disabled = busy || offline || state !== 'scanning';
    port.disabled = state !== 'disconnected';
    const opt = port.selectedOptions[0];
    $('port-desc').textContent = opt ? opt.textContent : '';   // descrição completa (o select trunca)
    $('btn-refresh').disabled = state !== 'disconnected';

    const st = $('st-state');
    st.textContent = STATE_LABEL[state];
    st.dataset.state = state;
    const info = status && status.info;
    $('st-model').textContent = info ? info.model : '—';
    $('st-firmware').textContent = info ? info.firmware : '—';
    $('st-serial').textContent = info && info.serial ? info.serial : '—';
    const h = status && status.health;
    const hs = $('st-health');
    hs.textContent = h ? HEALTH_LABEL[h.status] + (h.error_code ? ` (código ${h.error_code})` : '') : '—';
    hs.dataset.health = h ? h.status : '';
    const errs = status ? status.errors : 0;
    const el = $('st-errors');
    el.textContent = errs;
    el.dataset.bad = errs > 0 ? '1' : '';
    showError();
  }

  function apply(newStatus) {
    const was = status ? status.state : 'disconnected';
    status = newStatus;
    offline = false;
    hooks.onState(status.state);
    if (was === 'scanning' && status.state !== 'scanning') hooks.onStopped();
    render();
  }

  async function poll() {
    try {
      apply(await api.status());
    } catch {
      offline = true;
      render();
    }
  }

  // Executa um comando: bloqueia os botões, mostra o erro (se houver) e re-sincroniza o estado.
  async function run(fn) {
    busy = true;
    cmdError = '';
    render();
    try {
      apply(await fn());
    } catch (e) {
      cmdError = e.message;
      await poll();
    } finally {
      busy = false;
      render();
    }
  }

  async function loadPorts() {
    const sel = $('port');
    const keep = sel.value;
    try {
      const { ports } = await api.ports();
      sel.replaceChildren(...ports.map((p) => {
        const o = new Option(p.label, p.id);
        o.title = p.label;
        return o;
      }));
      if (ports.some((p) => p.id === keep)) sel.value = keep;
    } catch (e) {
      cmdError = `não foi possível listar as portas: ${e.message}`;
    }
    render();
  }

  $('btn-connect').addEventListener('click', () => run(() => api.connect($('port').value)));
  $('btn-disconnect').addEventListener('click', () => run(() => api.disconnect()));
  $('btn-start').addEventListener('click', () => run(() => api.start()));
  $('btn-stop').addEventListener('click', () => run(() => api.stop()));
  $('btn-refresh').addEventListener('click', loadPorts);
  $('port').addEventListener('change', render);

  const applyRange = () => {
    const input = $('range');
    const v = parseFloat(input.value);
    if (!Number.isFinite(v) || v < 1 || v > 12) {
      cmdError = 'alcance inválido: use um valor entre 1 e 12 m';
      render();
      return;
    }
    cmdError = '';
    hooks.onRange(v);
    render();
  };
  $('btn-range').addEventListener('click', applyRange);
  $('range').addEventListener('keydown', (e) => { if (e.key === 'Enter') applyRange(); });

  render();
  loadPorts().then(poll);
  setInterval(poll, POLL_MS);
}
