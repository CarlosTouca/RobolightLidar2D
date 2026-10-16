"""Filtro e conversão polar -> cartesiano. Funções puras, sem I/O.

Convenção: 0° = frente (+Y), horário. Ver backend/models.py.
"""
from __future__ import annotations

import numpy as np

from .models import Frame, Scan


def scan_to_frame(scan: Scan) -> Frame:
    """Descarta pontos com qualidade 0 ou distância 0 e converte para x, y (mm inteiros)."""
    d = np.asarray(scan.distance_mm, dtype=np.float64)
    q = np.asarray(scan.quality)
    a = np.asarray(scan.angle_deg, dtype=np.float64)

    valid = (q > 0) & (d > 0)
    d, q, a = d[valid], q[valid], a[valid]

    theta = np.deg2rad(a)
    x = np.rint(d * np.sin(theta)).astype(np.int64)
    y = np.rint(d * np.cos(theta)).astype(np.int64)
    return Frame(x=x.tolist(), y=y.tolist(), q=q.astype(np.int64).tolist(), pose=scan.pose)
