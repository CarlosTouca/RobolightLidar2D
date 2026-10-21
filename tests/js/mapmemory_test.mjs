// Roda com `node tests/js/mapmemory_test.mjs` (o pytest chama; sai com código 1 se algo falhar).
import assert from 'node:assert/strict';
import { CELL_M, MapMemory, SHOW_AT } from '../../frontend/mapmemory.js';

const wall = (y, n = 40) => ({ xs: Array.from({ length: n }, (_, i) => -1 + i * 0.05), ys: Array(n).fill(y) });   // parede reta em y

// 1) parede: vista em varreduras seguidas, aparece; uma batida isolada não
const m = new MapMemory();
let w = wall(2.0);
m.add(w.xs, w.ys, w.xs.map(() => 30), 0, 0);
assert.equal(m.size, 0, 'uma única batida não é parede');
m.add(w.xs, w.ys, w.xs.map(() => 31), 0, 0);
assert.ok(m.size >= 38 && m.size <= 40, `parede confirmada (${m.size})`);
const a = m.arrays();
assert.equal(m.arrays(), a, 'cache reaproveitado');
assert.ok(Math.abs(a.y[0] - 2.0) < 1e-6 && a.q[0] === 31);

// 2) vários pontos na mesma célula na mesma varredura contam uma vez só
const dup = new MapMemory();
dup.add([1.0, 1.01, 1.02], [1.0, 1.0, 1.0], [5, 5, 5]);
assert.equal(dup.size, 0, 'três pontos na mesma célula e na mesma varredura = uma batida');

// 3) coisa que se move: aparece duas vezes, depois o raio atravessa o lugar e ela some
const mv = new MapMemory();
for (let s = 0; s < 2; s++) mv.add([0.0], [1.0], [9], 0, 0);               // objeto a 1 m, visto 2 vezes
assert.equal(mv.size, 1);
for (let s = 0; s < 3; s++) mv.add([0.0], [3.0], [9], 0, 0);               // agora o LiDAR vê a parede de trás: o raio atravessa
const ys = [...mv.arrays().y];
assert.ok(!ys.some((v) => Math.abs(v - 1.0) < 0.1), 'o raio que atravessa apaga a célula do objeto');
assert.ok(ys.some((v) => Math.abs(v - 3.0) < 0.1), 'e a parede de trás aparece');

// 4) ruído solto (um ponto novo por varredura) nunca se confirma
const noise = new MapMemory();
for (let s = 0; s < 50; s++) noise.add([s * 0.3 - 7], [3 + (s % 7) * 0.4], [1], 0, 0);   // um ponto sempre em célula nova
assert.equal(noise.size, 0);

// 5) a limpeza joga fora o não confirmado e antigo, e guarda a parede
const pr = new MapMemory();
w = wall(2.0);
for (let s = 0; s < 3; s++) pr.add(w.xs, w.ys, w.xs.map(() => 1), 0, 0);
const wallCells = pr.size;
for (let s = 0; s < 100; s++) pr.add([5 + s * 0.2], [-3], [1], 0, 0);     // lixo que nunca se repete
assert.equal(pr.size, wallCells, 'a parede continua');
assert.ok(pr.cells.size < wallCells + 60, `o lixo antigo foi descartado (${pr.cells.size} células)`);

// 6) negativos e células vizinhas não colidem; teto de células
const n = new MapMemory();
for (let s = 0; s < 2; s++) n.add([-1, 1, -1, 1], [-1, -1, 1, 1], [1, 2, 3, 4]);
assert.equal(n.size, 4);
const cap = new MapMemory(50);
for (let i = 0; i < 200; i++) cap.add([i * 0.2], [0], [1]);
assert.ok(cap.cells.size <= 50);

// 7) CSV em mm, só as paredes
const csv = new MapMemory();
for (let s = 0; s < 2; s++) csv.add([1.234], [-0.5], [42]);
csv.add([4.0], [4.0], [7]);                                                // visto uma vez: fora do CSV
const lines = csv.toCsv().trim().split('\n');
assert.equal(lines[1], 'x_mm,y_mm,qualidade');
assert.equal(lines.length, 3);
assert.equal(lines[2], '1234,-500,42');
csv.clear();
assert.equal(csv.size, 0);
assert.ok(SHOW_AT > 0 && CELL_M > 0);
console.log('mapmemory ok');
