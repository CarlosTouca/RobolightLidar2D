"""Escolhe a implementação de fonte pela porta selecionada e lista as portas.

O resto do sistema só usa LidarSource: a porta "SIMULADOR" vira o simulador;
qualquer outra (COMx) vira o driver real.
"""
from __future__ import annotations

from dataclasses import dataclass

from .base import LidarSource, SourceError
from .simulator import Simulator

SIM_PORT = Simulator.PORT   # "SIMULADOR"


@dataclass
class PortInfo:
    id: str
    label: str            # "COM7 — descrição [USB VID:PID]": o que o seletor mostra
    usb: bool = False


def format_port(device: str, description: str = "", vid: int | None = None,
                pid: int | None = None) -> PortInfo:
    """Rótulo legível: nome da porta + descrição do sistema (+ VID:PID se for USB).

    O Windows repete o nome da porta no fim da descrição ("... (COM7)"); isso é removido.
    """
    desc = (description or "").strip()
    if desc.endswith(f"({device})"):
        desc = desc[: -len(device) - 2].rstrip()
    if desc in ("", "n/a"):
        desc = "sem descrição"
    label = f"{device} — {desc}"
    usb = vid is not None and pid is not None
    if usb:
        label += f" [USB {vid:04X}:{pid:04X}]"
    return PortInfo(device, label, usb)


def list_serial_ports() -> list[PortInfo]:
    """Portas do sistema; as USB primeiro (o C1 vem por um adaptador USB-UART)."""
    try:
        from serial.tools import list_ports
        infos = [format_port(p.device, p.description, p.vid, p.pid) for p in list_ports.comports()]
    except Exception:    # sem pyserial ou erro do sistema: só o simulador fica disponível
        return []
    return sorted(infos, key=lambda i: (not i.usb, i.id))


def list_ports() -> list[PortInfo]:
    """SIMULADOR sempre presente, seguido das portas seriais do sistema."""
    return [PortInfo(SIM_PORT, "SIMULADOR (dados simulados)")] + list_serial_ports()


def create_source_for_port(port: str) -> LidarSource:
    if port == SIM_PORT:
        return Simulator()
    try:
        from .rplidar_c1 import RplidarC1   # etapa 6
    except ImportError as e:
        raise SourceError(
            f"driver do RPLIDAR C1 ainda não implementado; não é possível abrir {port}") from e
    return RplidarC1()
