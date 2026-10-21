"""Refaz a estimativa de posição OFFLINE com varreduras gravadas, para diagnosticar o mapa sem o sensor.

Na página, com o C1 conectado, ande com o rover e clique em "Baixar varreduras (diagnóstico)" (ou abra
http://127.0.0.1:8000/api/recording). Depois:
    .venv\Scripts\python scripts\replay_scans.py varreduras.ndjson
    .venv\Scripts\python scripts\replay_scans.py varreduras.ndjson --min-range 1.2 --png captures\replay.png

Mostra quantas vezes o alinhamento se perdeu, o caminho estimado e o que mudaria com outra zona cega, e (com --png)
desenha o mapa refeito ao lado do que foi gravado. Não abre nenhuma porta serial.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.localization import DEFAULT_MIN_RANGE_MM, PoseEstimator  # noqa: E402


def load(path: Path):
    with path.open(encoding="utf-8") as f:
        header = json.loads(f.readline())
        scans = [json.loads(line) for line in f if line.strip()]
    return header, scans


def replay(scans, min_range_mm: float):
    est, track, lost = PoseEstimator(min_range_mm), [], 0
    for s in scans:
        pose, ok = est.update(s["x"], s["y"])
        lost += not ok
        track.append(pose)
    return track, lost


def path_length(track) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(track, track[1:]))


def world_points(scans, track):
    out = []
    for s, (px, py, h) in zip(scans, track):
        c, sn = math.cos(math.radians(h)), math.sin(math.radians(h))
        x, y = np.asarray(s["x"], float), np.asarray(s["y"], float)
        out.append(np.column_stack([px + x * c + y * sn, py - x * sn + y * c]))
    return np.vstack(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("arquivo", type=Path)
    ap.add_argument("--min-range", type=float, default=DEFAULT_MIN_RANGE_MM / 1000, help="zona cega em metros")
    ap.add_argument("--png", type=Path, help="salva o mapa refeito (e o gravado) neste PNG")
    args = ap.parse_args(argv)

    header, scans = load(args.arquivo)
    print(f"{len(scans)} varreduras ({header.get('unidade')}), zona cega do replay: {args.min_range:g} m")
    recorded = [tuple(s["pose"]) if s.get("pose") else (0.0, 0.0, 0.0) for s in scans]
    print(f"gravado: alinhamento perdido em {sum(1 for s in scans if not s.get('ok', True))} varreduras, "
          f"caminho estimado {path_length(recorded) / 1000:.2f} m")
    for mr in sorted({args.min_range, 0.3, 0.8, 1.2, 1.6}):
        track, lost = replay(scans, mr * 1000)
        print(f"replay com zona cega {mr:4.1f} m: perdeu {lost:3d} | caminho {path_length(track) / 1000:6.2f} m | "
              f"afastou-se no máximo {max(math.hypot(p[0], p[1]) for p in track) / 1000:5.2f} m | "
              f"pose final ({track[-1][0]:.0f} mm, {track[-1][1]:.0f} mm, {track[-1][2]:.1f}°)")
    if args.png:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        track, _ = replay(scans, args.min_range * 1000)
        fig, axes = plt.subplots(1, 2, figsize=(14, 7), sharex=True, sharey=True)
        for ax, tr, title in ((axes[0], recorded, "gravado (o que a página mostrou)"),
                              (axes[1], track, f"replay, zona cega {args.min_range:g} m")):
            w = world_points(scans, tr)
            ax.scatter(w[:, 0] / 1000, w[:, 1] / 1000, s=1, alpha=.3)
            ax.plot([p[0] / 1000 for p in tr], [p[1] / 1000 for p in tr], "r-", lw=1)
            ax.set_title(title); ax.set_aspect("equal"); ax.grid(alpha=.3)
        args.png.parent.mkdir(exist_ok=True)
        fig.savefig(args.png, dpi=110)
        print(f"imagem salva em {args.png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
