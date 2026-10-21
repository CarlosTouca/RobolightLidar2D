"""Gravação das últimas varreduras (só quando a posição é estimada), para diagnosticar o mapa com dados reais.

Guarda os pontos BRUTOS (no referencial do sensor) e a posição que foi estimada na hora. O arquivo é NDJSON:
a primeira linha descreve o formato e cada linha seguinte é uma varredura. `scripts/replay_scans.py` refaz a
estimativa offline com o código atual, sem precisar do sensor.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterator

import numpy as np

HEADER = {"formato": 1, "unidade": "mm", "convencao": "x direita, y frente; 0° = +Y, horário",
          "campos": "t (s), x[], y[], q[] (referencial do sensor), pose [x, y, rumo°] estimada, ok"}


class ScanRecorder:
    def __init__(self, max_scans: int = 1200):          # ~2 min a 10 Hz, ~3 MB
        self._buf: deque = deque(maxlen=max_scans)

    def __len__(self) -> int:
        return len(self._buf)

    def add(self, t: float, frame, pose, ok: bool) -> None:
        self._buf.append((t, np.asarray(frame.x, dtype=np.int16), np.asarray(frame.y, dtype=np.int16),
                          np.asarray(frame.q, dtype=np.uint8), None if pose is None else tuple(map(float, pose)), ok))

    def clear(self) -> None:
        self._buf.clear()

    def lines(self) -> Iterator[str]:
        yield json.dumps(HEADER, ensure_ascii=False) + "\n"
        for t, x, y, q, pose, ok in list(self._buf):
            yield json.dumps({"t": round(t, 3), "x": x.tolist(), "y": y.tolist(), "q": q.tolist(),
                              "pose": None if pose is None else [round(v, 1) for v in pose], "ok": ok},
                             separators=(",", ":")) + "\n"
