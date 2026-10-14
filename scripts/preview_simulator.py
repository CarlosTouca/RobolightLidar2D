"""Script descartável: desenha a sala simulada (vista de cima) em matplotlib.

Uso:  python scripts/preview_simulator.py [--show] [--scans N] [--range M]
Sem --show salva scripts/preview_simulator.png.
Convenção: 0° = +Y (frente), horário. Cor = qualidade.
"""
import argparse
import sys
from pathlib import Path

import matplotlib
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.pipeline import scan_to_frame                            # noqa: E402
from backend.sources.simulator import default_scene, simulate_scan    # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--show", action="store_true")
ap.add_argument("--scans", type=int, default=1, help="varreduras sobrepostas")
ap.add_argument("--range", type=float, default=6.0, help="alcance dos eixos (m)")
args = ap.parse_args()

if not args.show:
    matplotlib.use("Agg")
import matplotlib.pyplot as plt                                       # noqa: E402

rng = np.random.default_rng(0)
scene = default_scene()
xs, ys, qs = [], [], []
for _ in range(args.scans):
    f = scan_to_frame(simulate_scan(scene, rng))
    xs += f.x
    ys += f.y
    qs += f.q
x, y = np.array(xs) / 1000, np.array(ys) / 1000

fig, ax = plt.subplots(figsize=(7, 7))
sc = ax.scatter(x, y, c=qs, s=6, cmap="viridis", vmin=0, vmax=63)
for r in range(1, int(args.range) + 1):
    ax.add_patch(plt.Circle((0, 0), r, fill=False, color="gray", lw=0.6, ls="--"))
    ax.text(0, r, f"{r} m", color="gray", fontsize=8, ha="center", va="bottom")
ax.plot(0, 0, "r^", ms=10, label="sensor (0° = +Y)")
ax.set_xlim(-args.range, args.range)
ax.set_ylim(-args.range, args.range)
ax.set_aspect("equal")
ax.grid(alpha=0.3)
ax.set_xlabel("x (m)  → direita")
ax.set_ylabel("y (m)  → frente")
ax.set_title(f"Simulador — {len(x)} pontos ({args.scans} varredura(s))")
fig.colorbar(sc, label="qualidade")
ax.legend(loc="lower right")

if args.show:
    plt.show()
else:
    out = ROOT / "scripts" / "preview_simulator.png"
    fig.savefig(out, dpi=110, bbox_inches="tight")
    print("salvo em", out)
