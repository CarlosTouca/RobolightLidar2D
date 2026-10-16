// Gráfico Plotly: layout construído uma vez; cada quadro só troca os dados.
//
// Convenção (igual ao backend): 0° = frente = +Y (para cima), sentido horário.
// O backend envia mm inteiros; aqui convertemos para metros só para exibir.

const Q_MAX = 63; // modo Standard do C1: qualidade 0..63
const STEP_MS = 16;   // passo da animação (~60 fps); setTimeout, não depende do requestAnimationFrame
const RING_LABEL_ANGLE = Math.SQRT1_2; // rótulos a 45° (cima-direita) de cada anel

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const CONFIG = {
  responsive: true,
  displaylogo: false,
  scrollZoom: true,
  doubleClick: 'reset', // duplo clique volta ao enquadramento inicial
  modeBarButtonsToRemove: ['select2d', 'lasso2d', 'toImage'],
};

function buildLayout(rangeM, revision) {
  const fg = css('--fg');
  const grid = css('--grid');
  const zero = css('--zero');
  const ring = css('--ring');

  const shapes = [];
  const annotations = [];
  for (let r = 1; r <= Math.floor(rangeM); r++) {
    shapes.push({
      type: 'circle', xref: 'x', yref: 'y', layer: 'below',
      x0: -r, y0: -r, x1: r, y1: r,
      line: { color: ring, width: 1, dash: 'dot' },
    });
    annotations.push({
      x: r * RING_LABEL_ANGLE, y: r * RING_LABEL_ANGLE, xref: 'x', yref: 'y',
      text: `${r} m`, showarrow: false, xanchor: 'left', yanchor: 'bottom',
      font: { size: 11, color: ring },
    });
  }

  const axis = (title) => ({
    title: { text: title, standoff: 8 },
    range: [-rangeM, rangeM],
    dtick: 1,
    constrain: 'domain',
    showgrid: true,
    gridcolor: grid,
    zeroline: true,
    zerolinecolor: zero,
    linecolor: zero,
    tickfont: { color: fg },
    ticks: 'outside',
    tickcolor: zero,
  });

  return {
    // Constante entre quadros (preserva zoom/pan do usuário). Só muda em setRange(),
    // quando o usuário pede explicitamente um novo enquadramento.
    uirevision: `lidar-${revision}`,
    autosize: true,
    margin: { l: 56, r: 24, t: 12, b: 48 },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: css('--plot-bg'),
    font: { color: fg, size: 12 },
    showlegend: false,
    dragmode: 'pan',
    hovermode: 'closest',
    xaxis: axis('x (m)  →  direita'),
    yaxis: { ...axis('y (m)  ↑  frente (0°)'), scaleanchor: 'x', scaleratio: 1 },
    shapes,
    annotations,
  };
}

// pose = [x mm, y mm, rumo°] do sensor como aparece na tela; a seta aponta para onde o sensor olha.
function sensorTrace([px, py, heading]) {
  return {
    type: 'scatter', // um único ponto: trace separado dos pontos medidos
    mode: 'markers',
    x: [px / 1000], y: [py / 1000],
    name: 'Sensor',
    hovertemplate: 'Sensor (%{x:.2f}, %{y:.2f}) m<extra></extra>',
    // Ponta de seta (como um cursor): a ponta mostra para onde o sensor olha (0° = +Y, horário).
    marker: { symbol: 'arrow', size: 30, angle: heading, color: css('--sensor'),
              line: { color: css('--fg'), width: 1 } },
  };
}

const DEG = Math.PI / 180;
// Gira (x, y) em `deg` graus no sentido horário (mesma convenção do projeto: 0° = +Y, horário).
const rotate = (x, y, deg) => {
  const c = Math.cos(deg * DEG), s = Math.sin(deg * DEG);
  return [x * c + y * s, -x * s + y * c];
};

export function createPlot(el, rangeM) {
  let revision = 0;
  let layout = buildLayout(rangeM, revision);
  let pointColor = css('--points');
  let last = null;                       // último {frame, byQuality} desenhado

  // Mundo x tela. Os pontos de cada quadro ficam no MUNDO (mapa fixo; o sensor anda). A tela mostra o mundo
  // girado em `rotShown` graus (analógico direito do joystick). Os dois valores que se movem entre quadros
  // (pose do sensor e rotação) deslizam até o alvo para a imagem não "pular" no ritmo dos quadros.
  let world = { x: new Float32Array(0), y: new Float32Array(0) };   // metros, no mundo
  let pose = [0, 0, 0];                  // alvo: última pose recebida; sem pose (sensor real) fica na origem
  let from = pose, shown = pose;         // pose mostrada agora e de onde o deslize começou
  let glideStart = 0, glideMs = 100, lastFrameAt = 0;
  let rotTarget = 0, rotShown = 0;       // rotação do mapa (graus, horário)
  let animating = false;
  let drawing = false;                   // Plotly.react em andamento: o deslize não compete com ele

  const viewPoints = () => {
    const n = world.x.length;
    const xs = new Float32Array(n), ys = new Float32Array(n);
    const c = Math.cos(rotShown * DEG), s = Math.sin(rotShown * DEG);
    for (let i = 0; i < n; i++) {
      xs[i] = world.x[i] * c + world.y[i] * s;
      ys[i] = -world.x[i] * s + world.y[i] * c;
    }
    return [xs, ys];
  };

  // Seta do sensor: posição e rumo (0° = +Y, horário) no MUNDO, levados à tela pela rotação do mapa.
  const sensorNow = () => {
    const [sx, sy] = rotate(shown[0] / 1000, shown[1] / 1000, rotShown);
    return sensorTrace([sx * 1000, sy * 1000, shown[2] + rotShown]);
  };

  // Troca de tema claro/escuro: reconstrói cores; o uirevision mantém o zoom atual.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    layout = buildLayout(rangeM, revision);   // mesma revisão: o zoom atual é mantido
    pointColor = css('--points');
    if (last) update(last.frame, last.byQuality);
  });

  // Animação (só roda enquanto há o que mover): avança a pose e a rotação e atualiza o gráfico com
  // Plotly.restyle (só x/y/ângulo; os quadros continuam chegando por Plotly.react).
  function step() {
    const t = Math.min(1, (performance.now() - glideStart) / glideMs);
    const dh = ((pose[2] - from[2] + 540) % 360) - 180;                    // giro pelo caminho mais curto
    shown = [from[0] + (pose[0] - from[0]) * t, from[1] + (pose[1] - from[1]) * t, (from[2] + dh * t + 360) % 360];
    const dr = ((rotTarget - rotShown + 540) % 360) - 180;
    const rotMoved = dr !== 0;
    rotShown = Math.abs(dr) < 0.05 ? rotTarget : rotShown + dr * 0.4;     // aproxima o alvo sem overshoot
    if (el.data && !drawing) {
      if (rotMoved) {
        const [xs, ys] = viewPoints();
        Plotly.restyle(el, { x: [xs], y: [ys] }, [0]);
      }
      const sn = sensorNow();
      Plotly.restyle(el, { x: [sn.x], y: [sn.y], 'marker.angle': [sn.marker.angle] }, [1]);
    }
    if (t < 1 || rotShown !== rotTarget) setTimeout(step, STEP_MS); else animating = false;
  }
  const kick = () => { if (!animating) { animating = true; setTimeout(step, STEP_MS); } };

  /** Desenha um quadro {x[], y[], q[], pose?} (mm). Sempre Plotly.react: o gráfico nunca é recriado.
   *  Os pontos vêm relativos ao sensor; com `pose` eles são levados ao mundo (mapa fixo, o sensor anda). */
  function update(frame, byQuality) {
    const hadFrame = !!(last && last.frame);
    last = { frame, byQuality };
    if (frame) {
      const now = performance.now();
      pose = frame.pose || [0, 0, 0];                              // fonte sem pose (sensor real): origem
      if (!hadFrame) { shown = from = pose; }
      else {
        // O deslize dura o tempo real entre quadros: sem sobra (seta parada) nem falta (seta atrasada).
        glideMs = Math.min(400, Math.max(40, now - lastFrameAt));
        from = shown;
        glideStart = now;
        kick();
      }
      lastFrameAt = now;
      const [px, py, heading] = pose;
      const c = Math.cos(heading * DEG), s = Math.sin(heading * DEG);
      const n = frame.x.length;
      world = { x: new Float32Array(n), y: new Float32Array(n) };
      for (let i = 0; i < n; i++) {
        const x = frame.x[i], y = frame.y[i];
        world.x[i] = (px + x * c + y * s) / 1000;
        world.y[i] = (py - x * s + y * c) / 1000;
      }
    } else {
      world = { x: new Float32Array(0), y: new Float32Array(0) };
    }
    const n = world.x.length;
    const [xs, ys] = viewPoints();
    const points = {
      type: 'scattergl',
      mode: 'markers',
      x: xs, y: ys,
      hovertemplate: 'x %{x:.2f} m<br>y %{y:.2f} m<extra></extra>',
      marker: byQuality && n
        ? {
            size: 4, color: frame.q, colorscale: 'Viridis', cmin: 0, cmax: Q_MAX,
            showscale: true,
            colorbar: { title: { text: 'qualidade' }, thickness: 10, len: 0.5, tickfont: { color: css('--fg') } },
          }
        : { size: 4, color: pointColor, showscale: false },
    };
    drawing = true;
    return Plotly.react(el, [points, sensorNow()], layout, CONFIG)
      .finally(() => { drawing = false; });
  }

  /** Novo alcance (±m): refaz anéis/eixos e redefine o enquadramento. */
  function setRange(m) {
    rangeM = m;
    revision += 1;                       // única mudança de uirevision
    layout = buildLayout(rangeM, revision);
    return last ? update(last.frame, last.byQuality) : update(null, true);
  }

  /** Gira o mapa na tela (graus, horário); a imagem desliza até lá. */
  function setRotation(deg) {
    const next = ((deg % 360) + 360) % 360;
    if (next === rotTarget) return;      // chamado a cada leitura do controle: não anima à toa
    rotTarget = next;
    kick();
  }

  return { update, setRange, setRotation };
}
