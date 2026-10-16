// Gráfico Plotly: layout construído uma vez; cada quadro só troca os dados.
//
// Convenção (igual ao backend): 0° = frente = +Y (para cima), sentido horário.
// O backend envia mm inteiros; aqui convertemos para metros só para exibir.

const Q_MAX = 63; // modo Standard do C1: qualidade 0..63
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

// Rover visto de cima (mm; x para a direita, y para a frente). A escala é só visual (o rover simulado tem 25 cm de raio).
const ROVER_SCALE = 1.05;   // 0,75 x 1,4 (40% maior que a versão anterior)
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

// Próximo quadro de animação. Se o navegador não entregar o requestAnimationFrame (aba oculta, painel embutido),
// um temporizador assume em até 50 ms: a animação nunca fica presa esperando por ele.
const nextFrame = (fn) => {
  let done = false;
  const run = () => { if (!done) { done = true; fn(); } };
  requestAnimationFrame(run);
  setTimeout(run, 50);
};

export function createPlot(el, rangeM) {
  let revision = 0;
  let layout = buildLayout(rangeM, revision);
  let pointColor = css('--points');
  let last = null;                       // último {frame, byQuality} desenhado

  // Mundo x tela. Os pontos de cada quadro ficam no MUNDO (mapa fixo; o rover anda). A tela mostra o mundo
  // girado em `rotShown` graus (analógico direito do joystick). Os dois valores que se movem entre mensagens
  // (pose do rover e rotação) deslizam até o alvo para a imagem não "pular" no ritmo das mensagens.
  let world = { x: new Float32Array(0), y: new Float32Array(0) };   // metros, no mundo
  let pose = [0, 0, 0];                  // alvo: última pose recebida; sem pose (sensor real) fica na origem
  let from = pose, shown = pose;         // pose mostrada agora e de onde o deslize começou
  let havePose = false;                  // já veio alguma pose do servidor (rover simulado)?
  let glideStart = 0, glideMs = 17, lastPoseAt = 0;
  let rotTarget = 0, rotShown = 0;       // rotação do mapa (graus, horário)
  let animating = false;
  let drawing = false;                   // Plotly.react em andamento: o giro dos pontos espera por ele
  let hooked = false;

  // O rover é desenhado num canvas POR CIMA do gráfico, fora do Plotly: restyle/react custam dezenas de ms
  // e faziam o movimento travar. Assim ele anda na taxa da tela, sem depender do que o Plotly está fazendo.
  const cv = document.createElement('canvas');
  cv.style.cssText = 'position:absolute;left:0;top:0;pointer-events:none';
  el.style.position = 'relative';
  let colors = null;
  const readColors = () => ({ body: css('--sensor'), nose: css('--warn'), line: css('--fg'), dot: css('--bar-bg') });

  function drawRover() {
    const fl = el._fullLayout;
    const xa = fl && fl.xaxis, ya = fl && fl.yaxis;
    if (!xa || !ya || !xa._length) return;                          // gráfico ainda não desenhado
    if (cv.parentNode !== el) el.appendChild(cv);
    const w = el.clientWidth, h = el.clientHeight, dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
      cv.style.width = `${w}px`; cv.style.height = `${h}px`;
    }
    colors = colors || readColors();
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.beginPath(); ctx.rect(xa._offset, ya._offset, xa._length, ya._length); ctx.clip();   // só dentro dos eixos
    const px = (x) => xa._offset + xa.d2p(x), py = (y) => ya._offset + ya.d2p(y);              // metros -> pixels
    const [cx, cy] = rotate(shown[0] / 1000, shown[1] / 1000, rotShown);   // posição na tela (mapa girado)
    const deg = shown[2] + rotShown;
    const draw = (parts, fill) => {
      ctx.beginPath();
      for (const pts of parts) {
        pts.forEach(([x, y], i) => {
          const [rx, ry] = rotate(x * ROVER_SCALE, y * ROVER_SCALE, deg);
          const X = px(cx + rx / 1000), Y = py(cy + ry / 1000);
          if (i) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
        });
        ctx.closePath();
      }
      ctx.fillStyle = fill; ctx.fill();
      ctx.lineWidth = 1; ctx.strokeStyle = colors.line; ctx.stroke();
    };
    draw(ROVER_BODY, colors.body);
    draw(ROVER_WHEELS, '#4b5563');                                  // rodas: cinza escuro nos dois temas
    draw(ROVER_NOSE, colors.nose);
    ctx.beginPath(); ctx.arc(px(cx), py(cy), 3, 0, 2 * Math.PI);    // o sensor LiDAR, no centro
    ctx.fillStyle = colors.dot; ctx.fill(); ctx.lineWidth = 1.5; ctx.stroke();
    ctx.restore();
  }

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

  // Troca de tema claro/escuro: reconstrói cores; o uirevision mantém o zoom atual.
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    layout = buildLayout(rangeM, revision);   // mesma revisão: o zoom atual é mantido
    pointColor = css('--points');
    colors = null;
    if (last) update(last.frame, last.byQuality);
  });

  // Animação (só roda enquanto há o que mover): avança a pose e a rotação e redesenha o rover (canvas).
  // Girar o mapa também move os pontos, e isso sim passa pelo Plotly.restyle (só x/y).
  function step() {
    const t = Math.min(1, (performance.now() - glideStart) / glideMs);
    const dh = ((pose[2] - from[2] + 540) % 360) - 180;                    // giro pelo caminho mais curto
    shown = [from[0] + (pose[0] - from[0]) * t, from[1] + (pose[1] - from[1]) * t, (from[2] + dh * t + 360) % 360];
    const dr = ((rotTarget - rotShown + 540) % 360) - 180;
    const rotMoved = dr !== 0;
    rotShown = Math.abs(dr) < 0.05 ? rotTarget : rotShown + dr * 0.4;     // aproxima o alvo sem overshoot
    if (rotMoved && el.data && !drawing) {
      const [xs, ys] = viewPoints();
      Plotly.restyle(el, { x: [xs], y: [ys] }, [0]);
    }
    drawRover();
    if (t < 1 || rotShown !== rotTarget) nextFrame(step); else animating = false;
  }
  const kick = () => { if (!animating) { animating = true; nextFrame(step); } };

  /** Desenha um quadro {x[], y[], q[], pose?} (mm). Sempre Plotly.react: o gráfico nunca é recriado.
   *  Os pontos vêm relativos ao sensor; com `pose` eles são levados ao mundo (mapa fixo, o rover anda). */
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
    return Plotly.react(el, [points], layout, CONFIG).then((r) => {
      if (!hooked && el.on) {                       // zoom, pan e redimensionamento mudam a conversão metros -> pixels
        hooked = true;
        el.on('plotly_relayout', drawRover);
        el.on('plotly_afterplot', drawRover);
      }
      drawRover();
      return r;
    }).finally(() => { drawing = false; });
  }

  /** Pose atual do rover (canal rápido): o rover desliza do ponto anterior a este pelo tempo real entre
   *  mensagens, sem sobra (rover parado) nem falta (rover atrasado). */
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
    drawRover();
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
