// Memória do mapa: grade de ocupação que só mantém o que é PAREDE (o que se repete), não todo ponto visto.
//
// Cada célula de CELL_M guarda uma "confiança" (log-odds). Uma batida do LiDAR sobe a confiança; um raio que
// atravessa a célula (o LiDAR viu além dela) desce. Só células com confiança >= SHOW_AT aparecem:
//   - parede: é vista de novo a cada varredura e só sobe;
//   - ruído, pessoa passando, ponto fora do lugar (posição estimada errada): uma batida isolada nunca chega lá,
//     e se o raio depois atravessa o lugar, a célula é apagada;
//   - peça presa ao rover: cai em células diferentes a cada varredura, então nenhuma se confirma.
// Só faz sentido com posição conhecida (rover simulado, ou a estimativa do rover real).
export const CELL_M = 0.05;
const STRIDE = 2097152;      // |índice de célula| < 1.048.576 (±52 km): a chave ix * STRIDE + iy é única
const HIT = 0.9;             // batida (no máximo uma por célula por varredura)
const FREE = 0.35;           // raio atravessando a célula (no máximo uma vez por varredura)
const LO_MAX = 6, LO_MIN = -2;
export const SHOW_AT = 1.7;  // duas batidas seguidas bastam (2 x 0,9)
const STALE_SCANS = 30;      // célula ainda não confirmada e sem batida há tantas varreduras é descartada
const PRUNE_EVERY = 25;

export class MapMemory {
  constructor(max = 120000) {
    this.max = max;                // teto de células (confirmadas ou não), emergência
    this.cells = new Map();        // chave -> {lo, n, sx, sy, q, hitAt, freeAt}
    this.scan = 0;
    this._cache = null;
  }

  /** Quantas células de parede aparecem. */
  get size() { return this.arrays().x.length; }

  /** xs, ys em metros (mundo); qs = qualidade; (ox, oy) = onde o sensor estava (metros, mundo). */
  add(xs, ys, qs, ox = 0, oy = 0) {
    this.scan++;
    const c0x = Math.round(ox / CELL_M), c0y = Math.round(oy / CELL_M);
    const hits = [];
    for (let i = 0; i < xs.length; i++) {
      const ix = Math.round(xs[i] / CELL_M), iy = Math.round(ys[i] / CELL_M);
      this._carve(c0x, c0y, ix, iy);
      hits.push(i, ix, iy);
    }
    for (let k = 0; k < hits.length; k += 3) {   // batidas depois dos raios, para a própria batida não ser apagada
      const i = hits[k], key = hits[k + 1] * STRIDE + hits[k + 2];
      let c = this.cells.get(key);
      if (!c) { c = { lo: 0, n: 0, sx: 0, sy: 0, q: 0, hitAt: 0, freeAt: 0 }; this.cells.set(key, c); }
      if (c.hitAt !== this.scan) {
        c.hitAt = this.scan;
        c.lo = Math.min(LO_MAX, c.lo + HIT);
      }
      if (c.n >= 20) { c.sx *= 0.9; c.sy *= 0.9; c.n *= 0.9; }   // posição = média móvel dos pontos da célula
      c.sx += xs[i]; c.sy += ys[i]; c.n += 1; c.q = qs[i];
    }
    if (this.scan % PRUNE_EVERY === 0) this._prune();
    while (this.cells.size > this.max) this.cells.delete(this.cells.keys().next().value);
    this._cache = null;
  }

  // Bresenham da célula do sensor até a da batida (sem incluí-la): cada célula existente vira "livre" uma vez.
  _carve(x0, y0, x1, y1) {
    const dx = Math.abs(x1 - x0), dy = Math.abs(y1 - y0);
    const sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1;
    let err = dx - dy, x = x0, y = y0;
    while (x !== x1 || y !== y1) {
      const c = this.cells.get(x * STRIDE + y);
      if (c && c.freeAt !== this.scan) { c.freeAt = this.scan; c.lo = Math.max(LO_MIN, c.lo - FREE); }
      const e2 = 2 * err;
      if (e2 > -dy) { err -= dy; x += sx; }
      if (e2 < dx) { err += dx; y += sy; }
    }
  }

  _prune() {
    for (const [key, c] of this.cells) {
      if (c.lo < SHOW_AT && c.hitAt < this.scan - STALE_SCANS) this.cells.delete(key);
    }
  }

  clear() { this.cells.clear(); this.scan = 0; this._cache = null; }

  /** {x, y, q}: as células de parede (Float32Array em metros; q em Uint8Array), reaproveitado até a memória mudar. */
  arrays() {
    if (!this._cache) {
      const shown = [...this.cells.values()].filter((c) => c.lo >= SHOW_AT);
      const x = new Float32Array(shown.length), y = new Float32Array(shown.length), q = new Uint8Array(shown.length);
      shown.forEach((c, i) => { x[i] = c.sx / c.n; y[i] = c.sy / c.n; q[i] = c.q; });
      this._cache = { x, y, q };
    }
    return this._cache;
  }

  /** CSV em milímetros, só as paredes; mesma convenção do projeto (x direita, y frente; 0° = +Y, horário). */
  toCsv() {
    const { x, y, q } = this.arrays();
    const lines = ['# Mapa do LiDAR 2D (só paredes): pontos do mundo em mm (x = direita, y = frente; ângulo 0° = +Y, horário)',
                   'x_mm,y_mm,qualidade'];
    for (let i = 0; i < x.length; i++) lines.push(`${Math.round(x[i] * 1000)},${Math.round(y[i] * 1000)},${q[i]}`);
    return `${lines.join('\n')}\n`;
  }
}
