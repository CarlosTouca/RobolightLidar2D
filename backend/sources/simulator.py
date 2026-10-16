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


def wall(p0: tuple[float, float], p1: tuple[float, float],
         gaps: tuple[tuple[float, float], ...] = ()) -> list[Segment]:
    """Parede reta (horizontal ou vertical) de p0 a p1, com vãos de porta `gaps` = ((a, b), ...)
    medidos ao longo do eixo da parede (x numa parede horizontal, y numa vertical)."""
    horizontal = p0[1] == p1[1]
    a0, a1 = (p0[0], p1[0]) if horizontal else (p0[1], p1[1])
    cuts = [a0] + [v for gap in sorted(gaps) for v in gap] + [a1]
    return [((s, p0[1]), (e, p0[1])) if horizontal else ((p0[0], s), (p0[0], e))
            for s, e in zip(cuts[::2], cuts[1::2])]


def house_scene() -> Scene:
    """Casa de 10 x 8 m: sala e cozinha embaixo, corredor no meio, dois quartos e um banheiro em cima,
    com portas de 0,7 a 1,4 m e alguns móveis. O rover começa na sala, em (0, 0)."""
    s: list[Segment] = []
    s += rect_segments(-4000, -3000, 6000, 5000)                                  # paredes externas
    s += wall((-4000, 1000), (6000, 1000), ((-2800, -2000), (800, 2200)))         # corredor | cozinha e sala
    s += wall((-4000, 2400), (6000, 2400), ((-2800, -2000), (1000, 1800), (4300, 5000)))  # corredor | quartos e banheiro
    s += wall((-500, -3000), (-500, 1000), ((-1800, -800),))                      # cozinha | sala
    s += wall((0, 2400), (0, 5000))                                               # quarto 1 | quarto 2
    s += wall((3500, 2400), (3500, 5000))                                         # quarto 2 | banheiro
    for box in ((2600, -2800, 4600, -2100),      # sofá
                (1200, -1700, 2400, -900),       # mesa de centro
                (5600, -2000, 5900, 0),          # móvel da TV
                (-3900, -2900, -1000, -2300),    # bancada da cozinha
                (-3900, 3500, -2200, 4900),      # cama do quarto 1
                (-600, 3000, -50, 4800),         # guarda-roupa
                (200, 3600, 1800, 4900),         # cama do quarto 2
                (2500, 4400, 3400, 4900),        # escrivaninha
                (3650, 4200, 5900, 4900)):       # banheira
        s += rect_segments(*box)
    return Scene(segments=s, circles=[(-2000.0, -1200.0, 350.0), (3800.0, 3000.0, 150.0)])  # ilha, pia


def push_out(scene: Scene, x: float, y: float, radius: float) -> tuple[float, float]:
    """Afasta um disco de `radius` mm centrado em (x, y) das paredes e dos círculos da cena."""
    for _ in range(3):                           # alguns cantos exigem mais de uma correção
        moved = False
        for (ax, ay), (bx, by) in scene.segments:
            ex, ey = bx - ax, by - ay
            t = max(0.0, min(1.0, ((x - ax) * ex + (y - ay) * ey) / (ex * ex + ey * ey)))
            dx, dy = x - (ax + t * ex), y - (ay + t * ey)
            d = math.hypot(dx, dy)
            if d < radius:
                if d == 0:                       # exatamente sobre a parede: qualquer normal serve
                    dx, dy, d = -ey, ex, math.hypot(ex, ey)
                x, y, moved = x + dx / d * (radius - d), y + dy / d * (radius - d), True
        for cx, cy, r in scene.circles:
            dx, dy = x - cx, y - cy
            d = math.hypot(dx, dy)
            if d < r + radius:
                if d == 0:
                    dx, d = 1.0, 1.0
                x, y, moved = x + dx / d * (r + radius - d), y + dy / d * (r + radius - d), True
        if not moved:
            break
    return x, y


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
    return Scan(angle_deg=angles, distance_mm=d, quality=q, pose=pose)


class Simulator(LidarSource):
    name = "simulator"
    PORT = "SIMULADOR"
    SCAN_HZ = 10.0   # o C1 gira a ~10 Hz
    SPEED_MM_S = 1500.0  # velocidade do rover simulado a 100 % (escolha de visualização)
    TURN_DEG_S = 200.0   # giro máximo da seta (°/s)
    TURN_GAIN = 6.0      # o giro é proporcional ao erro (1/s): suave nos ajustes finos, até o máximo nas viradas grandes
    ROVER_RADIUS_MM = 250.0   # o rover é um disco: paredes e móveis o bloqueiam
    DRIVE_TIMEOUT_S = 0.8   # sem novo comando por este tempo, o rover para (igual ao firmware)

    def __init__(self, scene: Scene | None = None, seed: int | None = None,
                 health: Health | None = None, **scan_kwargs):
        self._health = health or Health(HealthStatus.OK)
        self._fault: str | None = None
        self._scene = scene or house_scene()
        self._rng = np.random.default_rng(seed)
        self._scan_kwargs = scan_kwargs
        self._opened = False
        self._scanning = threading.Event()
        # Rover simulado estilo mouse: a direção pedida é na tela (x direita, y cima); ele gira até lá e anda.
        self._lock = threading.Lock()    # a leitura (10 Hz) e o ticker de pose (30 Hz) integram o movimento
        self._last_t = time.monotonic()
        self._pose = [0.0, 0.0, 0.0]     # x mm, y mm, rumo°
        self._steer = (0.0, 0.0, 0.0)    # (x, y, velocidade 0..1): direção pedida na tela e quão rápido
        self._cmd_at = 0.0               # instante do último steer()

    def steer(self, x: float, y: float, speed: float) -> None:
        mag = min(1.0, math.hypot(x, y))
        ang = math.atan2(x, y)           # rumo pedido: 0° = +Y (cima), horário
        self._steer = (math.sin(ang) * mag, math.cos(ang) * mag, max(0.0, min(1.0, speed)))
        self._cmd_at = time.monotonic()

    def _advance(self, dt: float) -> None:
        # "Homem morto": sem comando novo por DRIVE_TIMEOUT_S, o rover para (cliente caiu ou o "parar" se perdeu).
        sx, sy, speed = self._steer if time.monotonic() - self._cmd_at <= self.DRIVE_TIMEOUT_S else (0.0, 0.0, 0.0)
        x, y, h = self._pose
        mag = math.hypot(sx, sy)
        if mag > 0:
            n = max(1, math.ceil(dt / 0.05))     # passos curtos: o rover nunca "pula" uma parede
            for _ in range(n):
                d = dt / n
                err = (math.degrees(math.atan2(sx, sy)) - h + 180.0) % 360.0 - 180.0   # [-180, 180)
                rate = max(-self.TURN_DEG_S, min(self.TURN_DEG_S, err * self.TURN_GAIN))
                step = max(-abs(err), min(abs(err), rate * d))          # nunca passa do alvo
                h = (h + step) % 360.0
                # Anda na direção em que a seta aponta; se ainda está virando muito, anda menos (ou nada).
                v = mag * speed * self.SPEED_MM_S * d * max(0.0, math.cos(math.radians(err - step)))
                x += v * math.sin(math.radians(h))
                y += v * math.cos(math.radians(h))
                x, y = push_out(self._scene, x, y, self.ROVER_RADIUS_MM)
        self._pose = [x, y, h]

    def _integrate(self) -> tuple[float, float, float]:
        """Avança o movimento até agora (relógio real) e devolve a pose. Seguro para duas threads."""
        with self._lock:
            now = time.monotonic()
            self._advance(min(now - self._last_t, 0.25))
            self._last_t = now
            return tuple(self._pose)

    def pose(self) -> tuple[float, float, float]:
        return self._integrate() if self._scanning.is_set() else tuple(self._pose)

    def open(self, port: str) -> None:
        if port != self.PORT:
            raise SourceError(f"porta inválida para o simulador: {port!r}")
        self._opened = True
        self._pose, self._steer = [0.0, 0.0, 0.0], (0.0, 0.0, 0.0)

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
        self._last_t = time.monotonic()     # sem "dívida" de tempo da pausa anterior
        self._scanning.set()

    def stop_scan(self) -> None:
        self._scanning.clear()

    def scans(self) -> Iterator[Scan]:
        period = 1.0 / self.SCAN_HZ
        deadline = time.monotonic()
        while self._scanning.is_set():
            self._raise_if_faulted()
            pose = self._integrate()
            yield simulate_scan(self._scene, self._rng, pose=pose, **self._scan_kwargs)
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
