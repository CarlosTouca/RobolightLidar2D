"""Posição do rover só pelo LiDAR: casamento de varreduras (ICP ponto-a-reta). EXPERIMENTAL.

O C1 não informa onde está; sem posição, varreduras de lugares diferentes não se alinham e o mapa não se forma.
Aqui a posição é ESTIMADA comparando cada varredura com uma varredura de referência ("quadro-chave"):
acha o deslocamento e o giro que fazem as duas se sobreporem e acumula isso.

Convenção do projeto (backend/models.py): 0° = +Y, ângulo cresce no sentido horário; a pose é (x mm, y mm, rumo°)
e começa em (0, 0, 0), onde o sensor estava ao ligar. Internamente o ICP usa o giro matemático (anti-horário);
a conversão está só em `PoseEstimator.update`.

Limites conhecidos (não são bugs): em corredor comprido e liso nada diz o quanto o rover andou ao longo dele,
então a estimativa para de avançar nesse eixo; erros pequenos se acumulam (deriva) sem fechamento de laço;
giros muito rápidos entre duas varreduras (mais de ~15°) podem perder o alinhamento.
"""
from __future__ import annotations

import math

import numpy as np

MAX_RANGE_MM = 8000.0          # pontos mais longos são esparsos e ruidosos demais para casar
DEFAULT_MIN_RANGE_MM = 800.0   # "zona cega": o que está mais perto é do próprio rover (mastro, cabos, bateria) e anda junto com o
                               # sensor; esses pontos parecem imóveis e fazem o casamento achar que o rover não saiu do lugar
NEIGHBOR_GAP_MM = 200.0        # vizinhos mais distantes que isso não formam uma parede (descontinuidade)
KEYFRAME_MM, KEYFRAME_DEG = 100.0, 5.0   # novo quadro-chave quando o rover se afastou tanto do atual
MAX_ITER = 20
MAX_POINTS = 240               # usa no máximo isso de pontos da varredura nova (o custo cresce com o quadrado)
TOL_MM, TOL_RAD = 0.1, 2e-6     # para quando o ajuste fica menor que isso
START_DIST_MM, MIN_DIST_MM = 400.0, 80.0  # distância máxima de pareamento: começa larga e vai fechando
TRIM = 0.85                    # usa só os 85% melhores pares (descarta objetos que se moveram, ruído)
INLIER_MIN = 0.35              # fração mínima de pontos pareados para confiar no resultado
RMS_MAX_MM = 60.0              # erro médio máximo (distância à reta) para confiar
LOST_RESET = 5                 # depois de tantas falhas seguidas, recomeça do ponto atual


def surface_normals(pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Normais por ponto (pontos em ordem de ângulo): perpendicular ao segmento entre os vizinhos.
    Devolve (normais Nx2, válidas N); fica inválido onde o vizinho está longe (borda de objeto)."""
    prev, nxt = np.roll(pts, 1, axis=0), np.roll(pts, -1, axis=0)
    t = nxt - prev
    n = np.stack([-t[:, 1], t[:, 0]], axis=1)
    norm = np.linalg.norm(n, axis=1)
    gap = np.maximum(np.linalg.norm(pts - prev, axis=1), np.linalg.norm(nxt - pts, axis=1))
    ok = (norm > 1e-6) & (gap < NEIGHBOR_GAP_MM)
    n = n / np.where(norm > 1e-6, norm, 1.0)[:, None]
    return n, ok


def match(new: np.ndarray, ref: np.ndarray, normals: np.ndarray, ok: np.ndarray,
          init: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    """Acha T = (tx mm, ty mm, th rad anti-horário) tal que R(th) · novo + t ≈ ref.
    Devolve (T, fração de pontos usados, erro médio mm) ou None se não houver pontos suficientes."""
    tx, ty, th = init
    max_d = START_DIST_MM
    n_new = len(new)
    idx = np.arange(n_new)
    inliers, rms = 0.0, float("inf")
    for _ in range(MAX_ITER):
        c, s = math.cos(th), math.sin(th)
        p = new @ np.array([[c, s], [-s, c]]) + (tx, ty)           # R(th) p + t
        d2 = ((p[:, None, :] - ref[None, :, :]) ** 2).sum(-1, dtype=np.float32)
        j = d2.argmin(1)
        d = np.sqrt(d2[idx, j])
        mask = ok[j] & (d < max_d)
        if mask.sum() < 20:
            return None
        mask &= d <= np.quantile(d[mask], TRIM)
        pp, q, n = p[mask], ref[j[mask]], normals[j[mask]]
        r = ((pp - q) * n).sum(1)
        a = np.column_stack([n[:, 0], n[:, 1], n[:, 1] * pp[:, 0] - n[:, 0] * pp[:, 1]])
        damp = 1e-3 * len(r)
        h = a.T @ a + np.diag([damp, damp, damp * 1e6])            # amortece eixos sem informação (corredor liso)
        dtx, dty, dth = -np.linalg.solve(h, a.T @ r)
        c2, s2 = math.cos(dth), math.sin(dth)                      # T <- delta ∘ T
        tx, ty = c2 * tx - s2 * ty + dtx, s2 * tx + c2 * ty + dty
        th += dth
        inliers, rms = mask.sum() / n_new, float(np.sqrt(np.mean(r ** 2)))
        max_d = max(MIN_DIST_MM, max_d * 0.8)
        if abs(dtx) < TOL_MM and abs(dty) < TOL_MM and abs(dth) < TOL_RAD:
            break
    return (tx, ty, th), inliers, rms


class PoseEstimator:
    """Uma varredura por vez, em mm no referencial do sensor (x direita, y frente), em ordem de ângulo."""

    def __init__(self, min_range_mm: float = DEFAULT_MIN_RANGE_MM):
        self.min_range_mm = min_range_mm
        self.reset()

    def reset(self) -> None:
        self.pose = (0.0, 0.0, 0.0)          # x mm, y mm, rumo° (horário) no mundo
        self.ok = True                       # a última varredura casou bem?
        self._ref = None                     # (pontos, normais, válidas, pose do quadro-chave)
        self._rel = (0.0, 0.0, 0.0)          # último T relativo ao quadro-chave (chute inicial da próxima)
        self._lost = 0

    def set_min_range(self, mm: float) -> None:
        """Muda a zona cega. Descarta o quadro-chave (foi feito com a zona antiga); a posição é mantida."""
        self.min_range_mm = float(mm)
        self._ref = None
        self._lost = 0

    def _keyframe(self, pts: np.ndarray) -> None:
        n, ok = surface_normals(pts)
        self._ref = (pts, n, ok, self.pose)
        self._rel = (0.0, 0.0, 0.0)

    def update(self, xs, ys) -> tuple[tuple[float, float, float], bool]:
        pts = np.column_stack([xs, ys]).astype(float)
        dist = np.hypot(pts[:, 0], pts[:, 1])
        pts = pts[(dist >= self.min_range_mm) & (dist <= MAX_RANGE_MM)]
        if len(pts) < 50:                                            # quase nada à vista: não dá para casar
            self.ok = False
            return self.pose, False
        if self._ref is None:
            self._keyframe(pts)
            return self.pose, True

        ref, normals, valid, (rx, ry, rh) = self._ref
        stride = max(1, len(pts) // MAX_POINTS)
        res = match(pts[::stride], ref, normals, valid, self._rel)
        if res is None or res[1] < INLIER_MIN or res[2] > RMS_MAX_MM:
            self.ok = False
            self._lost += 1
            if self._lost >= LOST_RESET:                             # perdeu de vez: recomeça daqui (pose mantida)
                self._keyframe(pts)
                self._lost = 0
            return self.pose, False

        (tx, ty, th), _, _ = res
        self._lost, self.ok, self._rel = 0, True, (tx, ty, th)
        h = math.radians(rh)                                         # mundo = pose do quadro-chave ∘ T (rumo horário)
        x = rx + tx * math.cos(h) + ty * math.sin(h)
        y = ry - tx * math.sin(h) + ty * math.cos(h)
        self.pose = (x, y, (rh - math.degrees(th)) % 360.0)
        if math.hypot(tx, ty) > KEYFRAME_MM or abs(math.degrees(th)) > KEYFRAME_DEG:
            self._keyframe(pts)
        return self.pose, True
