// Memória do mapa: guarda os pontos do MUNDO já vistos, para o mapa ir se formando enquanto o rover anda.
// Uma célula de CELL_M por ponto (o mais recente vence), então ficar parado ou passar de novo pelo mesmo lugar
// não incha a memória. Passando de `max` pontos, os mais antigos saem primeiro.
// Só faz sentido com posição conhecida (rover simulado, ou no futuro a estimativa do rover real).
export const CELL_M = 0.05;
const STRIDE = 2097152;   // |índice de célula| < 1.048.576 (±52 km): a chave ix * STRIDE + iy é única

export class MapMemory {
  constructor(max = 40000) {
    this.max = max;
    this.cells = new Map();   // chave -> [x m, y m, qualidade]; a ordem de inserção é a ordem de idade
    this._cache = null;
  }

  get size() { return this.cells.size; }

  /** xs, ys em metros (mundo); qs = qualidade de cada ponto. */
  add(xs, ys, qs) {
    for (let i = 0; i < xs.length; i++) {
      const key = Math.round(xs[i] / CELL_M) * STRIDE + Math.round(ys[i] / CELL_M);
      this.cells.set(key, [xs[i], ys[i], qs[i]]);
    }
    while (this.cells.size > this.max) this.cells.delete(this.cells.keys().next().value);
    this._cache = null;
  }

  clear() { this.cells.clear(); this._cache = null; }

  /** {x, y} em Float32Array (metros), reaproveitado até a memória mudar. */
  arrays() {
    if (!this._cache) {
      const x = new Float32Array(this.cells.size), y = new Float32Array(this.cells.size);
      let i = 0;
      for (const [px, py] of this.cells.values()) { x[i] = px; y[i] = py; i++; }
      this._cache = { x, y };
    }
    return this._cache;
  }

  /** CSV em milímetros, mesma convenção do projeto: x para a direita, y para a frente (0° = +Y, horário). */
  toCsv() {
    const lines = ['# Mapa do LiDAR 2D: pontos do mundo em mm (x = direita, y = frente; ângulo 0° = +Y, horário)',
                   'x_mm,y_mm,qualidade'];
    for (const [x, y, q] of this.cells.values()) lines.push(`${Math.round(x * 1000)},${Math.round(y * 1000)},${q}`);
    return `${lines.join('\n')}\n`;
  }
}
