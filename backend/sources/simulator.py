"""Fonte simulada: sala com paredes, um círculo e uma peça em U, com ruído.

Convenção (ver backend/models.py): 0° = +Y (frente), sentido horário,
x = d·sin(θ), y = d·cos(θ). O sensor fica na origem.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

from ..models import DeviceInfo, Health, HealthStatus, Scan
from .base import LidarSource, SourceError

Segment = tuple[tuple[float, float], tuple[float, float]]


@dataclass
class Scene:
    segments: list[Segment] = field(default_factory=list)                    # mm
    circles: list[tuple[float, float, float]] = field(default_factory=list)  # (cx, cy, r) mm


def rect_segments(x0: float, y0: float, x1: float, y1: float) -> list[Segment]:
    c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def u_shape(center: tuple[float, float], w: float, h: float, t: float,
            rot_deg: float = 0.0) -> list[Segment]:
    """Contorno de uma peça em U. Localmente: fundo em x∈[0,t], braços em y∈[0,t]
    e y∈[h-t,h], abertura voltada para +X. Gira `rot_deg` graus (anti-horário,
    matemático) em torno do centro e posiciona o centro em `center`."""
    pts = [(0, 0), (w, 0), (w, t), (t, t), (t, h - t), (w, h - t), (w, h), (0, h)]
    a = np.deg2rad(rot_deg)
    ca, sa = np.cos(a), np.sin(a)
    out = []
    for px, py in pts:
        dx, dy = px - w / 2, py - h / 2
        out.append((center[0] + dx * ca - dy * sa, center[1] + dx * sa + dy * ca))
    return [(out[i], out[(i + 1) % len(out)]) for i in range(len(out))]


def default_scene() -> Scene:
    """Sala de 6,0 x 5,0 m (sensor fora do centro), círculo de 30 cm de raio e
    um U cuja abertura aponta aproximadamente para o sensor."""
    segs = rect_segments(-2500, -2000, 3500, 3000)
    segs += u_shape(center=(-1800, 900), w=800, h=700, t=120, rot_deg=-26.6)
    return Scene(segments=segs, circles=[(1200.0, 1500.0, 300.0)])


def raycast(scene: Scene, angles_deg: np.ndarray) -> np.ndarray:
    """Distância (mm) do primeiro obstáculo em cada ângulo; inf se nada for atingido."""
    th = np.deg2rad(angles_deg)
    dx, dy = np.sin(th), np.cos(th)           # direção do raio
    best = np.full(th.shape, np.inf)

    for (px, py), (qx, qy) in scene.segments:
        ex, ey = qx - px, qy - py
        denom = dx * ey - dy * ex             # cross(dir, e)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (px * ey - py * ex) / denom   # cross(P, e) / denom
            s = (px * dy - py * dx) / denom   # cross(P, dir) / denom
        hit = (np.abs(denom) > 1e-12) & (t > 0) & (s >= 0) & (s <= 1)
        best = np.where(hit & (t < best), t, best)

    for cx, cy, r in scene.circles:
        b = dx * cx + dy * cy
        c = cx * cx + cy * cy - r * r
        disc = b * b - c
        with np.errstate(invalid="ignore"):
            t = b - np.sqrt(disc)
        hit = (disc >= 0) & (t > 0)
        best = np.where(hit & (t < best), t, best)

    return best


def seen_from(scene: Scene, x: float, y: float) -> Scene:
    """A mesma cena com a origem movida para (x, y) mm (o raycast parte sempre de (0, 0))."""
    return Scene(
        segments=[((a - x, b - y), (c - x, d - y)) for (a, b), (c, d) in scene.segments],
        circles=[(cx - x, cy - y, r) for cx, cy, r in scene.circles])


def simulate_scan(scene: Scene, rng: np.random.Generator, n: int = 500,
                  noise_mm: float = 10.0, angle_jitter_deg: float = 0.1,
                  dropout: float = 0.02, max_range_mm: float = 12000.0,
                  min_range_mm: float = 150.0,
                  pose: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> Scan:
    """Uma volta completa. Pontos fora de alcance ou perdidos saem com d=0 e q=0,
    como no sensor real, para exercitar o filtro do pipeline.

    `pose` = (x mm, y mm, rumo°) do sensor na cena; rumo segue a convenção do projeto
    (0° = +Y, horário). Os ângulos devolvidos são sempre relativos ao sensor."""
    angles = np.arange(n) * (360.0 / n)
    if angle_jitter_deg > 0:
        angles = angles + rng.normal(0.0, angle_jitter_deg, n)
    angles = np.mod(angles, 360.0)

    px, py, heading = pose
    if (px, py) != (0.0, 0.0):
        scene = seen_from(scene, px, py)
    d = raycast(scene, np.mod(angles + heading, 360.0))
    if noise_mm > 0:
        d = d + rng.normal(0.0, noise_mm, n)

    # Qualidade (modo Standard: 0..63) cai com a distância.
    q = np.clip(63 - d / 250.0 + rng.integers(-3, 4, n), 1, 63)

    invalid = ~np.isfinite(d) | (d > max_range_mm) | (d < min_range_mm)
    if dropout > 0:
        invalid |= rng.random(n) < dropout
    d = np.where(invalid, 0.0, d)
    q = np.where(invalid, 0, q).astype(np.int64)
    return Scan(angle_deg=angles, distance_mm=d, quality=q)


class Simulator(LidarSource):
    name = "simulator"
    PORT = "SIMULADOR"
    SCAN_HZ = 10.0   # o C1 gira a ~10 Hz
    SPEED_MM_S = 500.0   # velocidade do rover simulado com os dois lados a 100 %
    TURN_DEG_S = 90.0    # giro sobre o próprio eixo (lados opostos a 100 %)
    MARGIN_MM = 300.0    # folga mínima até as paredes

    def __init__(self, scene: Scene | None = None, seed: int | None = None,
                 health: Health | None = None, **scan_kwargs):
        self._health = health or Health(HealthStatus.OK)
        self._fault: str | None = None
        self._scene = scene or default_scene()
        self._rng = np.random.default_rng(seed)
        self._scan_kwargs = scan_kwargs
        self._opened = False
        self._scanning = threading.Event()
        # Rover simulado: tração diferencial (mesma lógica de lados do firmware do rover).
        xs = [p[0] for seg in self._scene.segments for p in seg]
        ys = [p[1] for seg in self._scene.segments for p in seg]
        self._bounds = (min(xs) + self.MARGIN_MM, max(xs) - self.MARGIN_MM,
                        min(ys) + self.MARGIN_MM, max(ys) - self.MARGIN_MM) if xs else None
        self._pose = [0.0, 0.0, 0.0]     # x mm, y mm, rumo° (só a thread de leitura escreve)
        self._cmd = (0.0, 0.0)           # (esquerda, direita), -1..1

    def drive(self, left: float, right: float) -> None:
        self._cmd = (max(-1.0, min(1.0, left)), max(-1.0, min(1.0, right)))

    def _advance(self, dt: float) -> None:
        left, right = self._cmd
        x, y, h = self._pose
        h = (h + (left - right) / 2 * self.TURN_DEG_S * dt) % 360.0   # dir < esq => horário
        v = (left + right) / 2 * self.SPEED_MM_S * dt
        x += v * math.sin(math.radians(h))
        y += v * math.cos(math.radians(h))
        if self._bounds:                  # shortcut: só limita à sala; círculo e U não bloqueiam
            x0, x1, y0, y1 = self._bounds
            x, y = min(max(x, x0), x1), min(max(y, y0), y1)
        self._pose = [x, y, h]

    @staticmethod
    def list_ports() -> list[str]:
        return [Simulator.PORT]

    def open(self, port: str) -> None:
        if port != self.PORT:
            raise SourceError(f"porta inválida para o simulador: {port!r}")
        self._opened = True
        self._pose, self._cmd = [0.0, 0.0, 0.0], (0.0, 0.0)

    def close(self) -> None:
        self.stop_scan()
        self._opened = False

    def inject_fault(self, kind: str = "disconnect") -> None:
        """Simula uma falha física. "disconnect": cabo puxado — a leitura em curso e qualquer
        chamada seguinte falham com SourceError, como o driver real faria."""
        if kind != "disconnect":
            raise ValueError(f"falha desconhecida: {kind!r}")
        self._fault = kind

    def set_health(self, health: Health) -> None:
        self._health = health

    def get_info(self) -> DeviceInfo:
        self._require_open()
        return DeviceInfo(model="SIMULADOR", firmware="1.0", hardware="0", serial="SIM-0001")

    def get_health(self) -> Health:
        self._require_open()
        return self._health

    def start_scan(self) -> None:
        self._require_open()
        if self._health.status is HealthStatus.ERROR:     # como o driver: Protection Stop
            raise SourceError(f"sensor em Protection Stop (código de erro {self._health.error_code}) "
                              f"e o RESET não resolveu (simulado)")
        self._scanning.set()

    def stop_scan(self) -> None:
        self._scanning.clear()

    def scans(self) -> Iterator[Scan]:
        period = 1.0 / self.SCAN_HZ
        deadline = last = time.monotonic()
        while self._scanning.is_set():
            self._raise_if_faulted()
            now = time.monotonic()
            self._advance(min(now - last, 0.25))
            last = now
            yield simulate_scan(self._scene, self._rng, pose=tuple(self._pose), **self._scan_kwargs)
            deadline += period
            delay = deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                deadline = time.monotonic()   # atrasado: não acumula dívida

    def _require_open(self) -> None:
        if not self._opened:
            raise SourceError("simulador não está aberto")
        self._raise_if_faulted()

    def _raise_if_faulted(self) -> None:
        if self._fault == "disconnect":
            raise SourceError("conexão perdida: cabo ou adaptador USB desconectado (falha simulada)")
