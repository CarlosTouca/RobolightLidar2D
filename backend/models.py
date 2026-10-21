"""Tipos de dados compartilhados.

Convenção de coordenadas (vale para todo o projeto):
  * 0° aponta para a FRENTE do sensor, que no gráfico é o eixo +Y (para cima).
  * O ângulo cresce no sentido HORÁRIO, visto de cima (90° = +X, à direita).
  * Portanto:  x = d * sin(θ),  y = d * cos(θ).
  * Distâncias em milímetros; o sensor fica na origem (0, 0).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

# (x mm, y mm, rumo°): onde o sensor está no mundo e para onde aponta. Os pontos de Scan/Frame
# continuam relativos ao sensor; quem desenha usa a pose para colocá-los no mundo.
# Rumo na convenção do projeto (0° = +Y, horário).
Pose = tuple[float, float, float]


@dataclass
class Scan:
    """Uma volta completa, em coordenadas polares brutas (como vêm do sensor)."""
    angle_deg: np.ndarray    # float, [0, 360)
    distance_mm: np.ndarray  # float, 0 = medida inválida
    quality: np.ndarray      # int, 0 = inválida (modo Standard: 0..63)
    pose: Pose | None = None  # só o simulador (rover simulado); o sensor real não informa

    def __post_init__(self) -> None:
        if not (len(self.angle_deg) == len(self.distance_mm) == len(self.quality)):
            raise ValueError("angle_deg, distance_mm e quality devem ter o mesmo tamanho")


@dataclass
class Frame:
    """Varredura pronta para envio: listas simples, sem objetos por ponto."""
    x: list[int]  # mm
    y: list[int]  # mm
    q: list[int]  # qualidade, usada para colorir
    pose: Pose | None = None  # posição do sensor no mundo (só o rover simulado informa)


class HealthStatus(str, Enum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class DeviceInfo:
    model: str
    firmware: str
    hardware: str = ""
    serial: str = ""


@dataclass
class Health:
    status: HealthStatus
    error_code: int = 0


@dataclass
class Rotation:
    """Velocidade de giro do sensor em voltas por segundo (Hz) e a faixa permitida."""
    hz: float
    min_hz: float
    max_hz: float


@dataclass
class Status:
    """Estado exposto no painel de controle."""
    state: str = "disconnected"   # disconnected | connected | scanning
    source: str = ""
    port: str = ""
    info: DeviceInfo | None = None
    health: Health | None = None
    points: int = 0
    errors: int = 0               # pacotes inválidos descartados pelo driver
    rotation: Rotation | None = None   # None: a fonte não permite mudar a velocidade de giro
    pose_mode: str = ""            # "fonte" (o simulador informa), "estimada" (só pelo LiDAR) ou "" (sem posição)
    pose_ok: bool = True           # com pose estimada: a última varredura casou bem?
    error: str = ""
