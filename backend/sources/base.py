"""Contrato único das fontes de dados (simulador e driver real).

O resto do sistema só conhece esta interface. Nada fora de `sources/`
deve importar pyserial.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..models import DeviceInfo, Health, Scan


class SourceError(Exception):
    """Falha de comunicação ou protocolo com a fonte."""


class LidarSource(ABC):
    name: str = ""

    @abstractmethod
    def open(self, port: str) -> None:
        """Conecta ao dispositivo. Levanta SourceError em caso de falha."""

    @abstractmethod
    def close(self) -> None:
        """Encerra a conexão (idempotente). Para a varredura se estiver ativa."""

    @property
    def error_count(self) -> int:
        """Pacotes inválidos descartados desde que a fonte foi aberta (0 se não se aplica)."""
        return 0

    def steer(self, x: float, y: float, speed: float) -> None:
        """Comando do rover simulado: direção desejada na tela (x direita, y cima, módulo até 1) e
        velocidade 0..1. Fontes reais ignoram."""

    @abstractmethod
    def get_info(self) -> DeviceInfo: ...

    @abstractmethod
    def get_health(self) -> Health: ...

    @abstractmethod
    def start_scan(self) -> None: ...

    @abstractmethod
    def stop_scan(self) -> None: ...

    @abstractmethod
    def scans(self) -> Iterator[Scan]:
        """Gera varreduras completas enquanto a varredura estiver ativa.

        Bloqueante; termina quando `stop_scan` ou `close` é chamado.
        """
