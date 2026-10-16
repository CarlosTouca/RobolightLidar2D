// Gráfico Plotly: layout construído uma vez; cada quadro só troca os dados.
//
// Convenção (igual ao backend): 0° = frente = +Y (para cima), sentido horário.
// O backend envia mm inteiros; aqui convertemos para metros só para exibir.

const Q_MAX = 63; // modo Standard do C1: qualidade 0..63
const GLIDE_STEP_MS = 16;
const GLIDE_MS = 200; // tempo entre quadros (MAX_FPS = 5): a seta desliza de uma pose à próxima nesse prazo
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

// pose = [x mm, y mm, rumo°] do sensor no mundo; a seta aponta para onde o sensor olha.
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

export function createPlot(el, rangeM) {
  let revision = 0;
  let layout = buildLayout(rangeM, revision);
  let pose = [0, 0, 0];   // última pose recebida; sem pose (sensor real) o sensor fica na origem
  let shown = pose;       // pose que a seta mostra agora: desliza até `pose` entre dois quadros
  let from = pose;
  let glideStart = 0;
  let gliding = false;
  let drawing = false;    // um Plotly.react em andamento: o deslize espera para não competir com ele
  let sensor = sensorTrace(shown);
  let pointColor = css('--points');

  // Troca de tema claro/escuro: reconstrói cores; o uirevision mantém o zoom atual.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    layout = buildLayout(rangeM, revision);   // mesma revisão: o zoom atual é mantido
    sensor = sensorTrace(shown);
    pointColor = css('--points');
    if (last) update(last.frame, last.byQuality);
  });

  let last = null;

  // Os quadros chegam a 5/s, então a seta só "pulava" 5 vezes por segundo. Entre dois quadros ela
  // desliza (~60 fps, com setTimeout: não depende do requestAnimationFrame) da pose anterior até a nova, só movendo o ponto do sensor (Plotly.restyle).
  function glide() {
    const t = Math.min(1, (performance.now() - glideStart) / GLIDE_MS);
    const dh = ((pose[2] - from[2] + 540) % 360) - 180;          // giro pelo caminho mais curto
    shown = [from[0] + (pose[0] - from[0]) * t, from[1] + (pose[1] - from[1]) * t,
             (from[2] + dh * t + 360) % 360];
    sensor = sensorTrace(shown);
    if (el.data && !drawing) {
      Plotly.restyle(el, { x: [[shown[0] / 1000]], y: [[shown[1] / 1000]], 'marker.angle': [shown[2]] }, [1]);
    }
    if (t < 1) setTimeout(glide, GLIDE_STEP_MS); else gliding = false;
  }

  /** Desenha um quadro {x[], y[], q[], pose?} (mm). Sempre Plotly.react: o gráfico nunca é recriado.
   *  Os pontos vêm relativos ao sensor; com `pose` eles são levados ao mundo (mapa fixo, o sensor anda). */
  function update(frame, byQuality) {
    const hadFrame = !!(last && last.frame);
    last = { frame, byQuality };
    if (frame) {
      const first = !hadFrame;                                     // primeiro quadro (ou após limpar)
      pose = frame.pose || [0, 0, 0];   // fonte sem pose (sensor real): fica na origem
      if (first) { shown = from = pose; } else {
        from = shown;
        glideStart = performance.now();
        if (!gliding) { gliding = true; setTimeout(glide, GLIDE_STEP_MS); }
      }
      sensor = sensorTrace(shown);
    }
    const [px, py, heading] = pose;
    const c = Math.cos((heading * Math.PI) / 180);
    const s = Math.sin((heading * Math.PI) / 180);
    const n = frame ? frame.x.length : 0;
    const xs = new Float32Array(n);
    const ys = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const x = frame.x[i], y = frame.y[i];
      xs[i] = (px + x * c + y * s) / 1000;
      ys[i] = (py - x * s + y * c) / 1000;
    }
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
    return Plotly.react(el, [points, sensor], layout, CONFIG)
      .finally(() => { drawing = false; });
  }

  /** Novo alcance (±m): refaz anéis/eixos e redefine o enquadramento. */
  function setRange(m) {
    rangeM = m;
    revision += 1;                       // única mudança de uirevision
    layout = buildLayout(rangeM, revision);
    return last ? update(last.frame, last.byQuality) : update(null, true);
  }

  return { update, setRange };
}
