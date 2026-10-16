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

// Rover visto de cima (mm; x para a direita, y para a frente). A escala é exagerada para dar para ver no mapa.
const ROVER_SCALE = 1.5;
const rect = (x0, y0, x1, y1) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]];
const ROVER_BODY = [rect(-130, -190, 130, 190)];
const ROVER_WHEELS = [rect(-175, -170, -130, -60), rect(-175, 60, -130, 170),
                      rect(130, -170, 175, -60), rect(130, 60, 175, 170)];
const ROVER_NOSE = [[[-70, 190], [70, 190], [0, 245], [-70, 190]]];   // a frente (câmera)

const DEG = Math.PI / 180;
// Gira (x, y) em `deg` graus no sentido horário (mesma convenção do projeto: 0° = +Y, horário).
const rotate = (x, y, deg) => {
  const c = Math.cos(deg * DEG), s = Math.sin(deg * DEG);
  return [x * c + y * s, -x * s + y * c];
};

// Polígonos do rover na tela: (cx, cy) = centro em metros (já girado pelo mapa); deg = rumo + giro do mapa.
function polygons(parts, cx, cy, deg) {
  const x = [], y = [];
  parts.forEach((pts, k) => {
    if (k) { x.push(null); y.push(null); }                       // vazio entre polígonos do mesmo traço
    for (const [px, py] of pts) {
      const [rx, ry] = rotate(px * ROVER_SCALE, py * ROVER_SCALE, deg);
      x.push(cx + rx / 1000);
      y.push(cy + ry / 1000);
    }
  });
  return { x, y };
}

// Traços do rover (índices 1 a 4 do gráfico): corpo, rodas, frente e o sensor LiDAR no centro.
function roverTraces(cx, cy, deg) {
  const solid = (parts, fill) => ({
    type: 'scatter', mode: 'lines', fill: 'toself', fillcolor: fill, hoverinfo: 'skip', showlegend: false,
    line: { color: css('--fg'), width: 1 }, ...polygons(parts, cx, cy, deg),
  });
  return [
    solid(ROVER_BODY, css('--sensor')),
    solid(ROVER_WHEELS, '#4b5563'),                              // rodas: cinza escuro nos dois temas
    solid(ROVER_NOSE, css('--warn')),
    {
      type: 'scatter', mode: 'markers', x: [cx], y: [cy], name: 'Sensor',
      hovertemplate: 'Sensor (%{x:.2f}, %{y:.2f}) m<extra></extra>',
      marker: { symbol: 'circle', size: 9, color: css('--bar-bg'), line: { color: css('--fg'), width: 2 } },
    },
  ];
}

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
  let havePose = false;                  // já veio alguma pose do servidor (rover simulado)?
  let glideStart = 0, glideMs = 33, lastPoseAt = 0;
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

  // Rover: posição e rumo (0° = +Y, horário) no MUNDO, levados à tela pela rotação do mapa.
  const roverNow = () => {
    const [cx, cy] = rotate(shown[0] / 1000, shown[1] / 1000, rotShown);
    return roverTraces(cx, cy, shown[2] + rotShown);
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
      const r = roverNow();
      Plotly.restyle(el, { x: r.map((tr) => tr.x), y: r.map((tr) => tr.y) }, [1, 2, 3, 4]);
    }
    if (t < 1 || rotShown !== rotTarget) setTimeout(step, STEP_MS); else animating = false;
  }
  const kick = () => { if (!animating) { animating = true; setTimeout(step, STEP_MS); } };

  /** Desenha um quadro {x[], y[], q[], pose?} (mm). Sempre Plotly.react: o gráfico nunca é recriado.
   *  Os pontos vêm relativos ao sensor; com `pose` eles são levados ao mundo (mapa fixo, o sensor anda). */
  function update(frame, byQuality) {
    last = { frame, byQuality };
    if (frame) {
      // Pontos: levados ao mundo com a pose DA VARREDURA. O rover usa a pose mais recente (setPose).
      if (!frame.pose && havePose) { havePose = false; pose = shown = from = [0, 0, 0]; }   // fonte sem pose (sensor real)
      else if (frame.pose && !havePose) setPose(frame.pose);                                // 1ª pose, se ainda não veio
      const [px, py, heading] = frame.pose || [0, 0, 0];
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
    return Plotly.react(el, [points, ...roverNow()], layout, CONFIG)
      .finally(() => { drawing = false; });
  }

  /** Pose atual do rover (canal rápido, ~30/s): o rover desliza do ponto anterior a este pelo tempo real
   *  entre mensagens, sem sobra (rover parado) nem falta (rover atrasado). */
  function setPose(p) {
    const now = performance.now();
    if (!havePose) { pose = shown = from = p; havePose = true; }
    else {
      glideMs = Math.min(150, Math.max(15, now - lastPoseAt));
      from = shown;
      glideStart = now;
      pose = p;
    }
    lastPoseAt = now;
    kick();
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

  return { update, setRange, setRotation, setPose };
}
