// Roda com `node tests/js/mapmemory_test.mjs` (o pytest chama; sai com código 1 se algo falhar).
import assert from 'node:assert/strict';
import { CELL_M, MapMemory } from '../../frontend/mapmemory.js';

const m = new MapMemory(5);
m.add([1.0, 1.01, 2.0], [0.0, 0.01, 0.0], [10, 20, 30]);            // 1.00 e 1.01 caem na mesma célula de 5 cm
assert.equal(m.size, 2);
assert.equal([...m.cells.values()].find((c) => c[0] < 1.5)[2], 20);  // o mais recente vence
m.add([1.0, 2.0], [0.0, 0.0], [11, 31]);                            // passar de novo pelo mesmo lugar não incha
assert.equal(m.size, 2);

const a = m.arrays();
assert.equal(a.x.length, 2);
assert.equal(m.arrays(), a);                                         // cache reaproveitado
m.add([5.0], [5.0], [1]);
assert.notEqual(m.arrays(), a);                                      // e refeito quando a memória muda

// negativos e células vizinhas não colidem
const n = new MapMemory();
n.add([-1, 1, -1, 1], [-1, -1, 1, 1], [1, 2, 3, 4]);
assert.equal(n.size, 4);
n.add([0, CELL_M], [0, 0], [1, 1]);
assert.equal(n.size, 6);

// teto: os mais antigos saem primeiro
const c = new MapMemory(3);
for (let i = 0; i < 6; i++) c.add([i], [0], [i]);
assert.equal(c.size, 3);
assert.deepEqual([...c.cells.values()].map((v) => v[2]), [3, 4, 5]);

// CSV em mm, com cabeçalho
const csv = new MapMemory();
csv.add([1.234], [-0.5], [42]);
const lines = csv.toCsv().trim().split('\n');
assert.equal(lines[1], 'x_mm,y_mm,qualidade');
assert.equal(lines[2], '1234,-500,42');
csv.clear();
assert.equal(csv.size, 0);
console.log('mapmemory ok');
