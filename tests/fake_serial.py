"""Serial falsa e dispositivo RPLIDAR simulado em nível de bytes, para testar o driver."""
from __future__ import annotations

import threading
import time

import serial

from backend.sources.rplidar_protocol import encode_measurement

DESC_INFO = bytes.fromhex("A5 5A 14 00 00 00 04")
DESC_HEALTH = bytes.fromhex("A5 5A 03 00 00 00 06")
DESC_SCAN = bytes.fromhex("A5 5A 05 00 00 40 81")        # exatamente como no manual [P22] p.16

INFO_DATA = bytes([0x41, 3, 1, 18]) + bytes(range(16))   # model, fw_minor, fw_major, hw, serial[16]


def health_data(status: int = 0, code: int = 0) -> bytes:
    return bytes([status, code & 0xFF, code >> 8])


def make_stream(n_scans: int, points: int = 500, dist=lambda i: 2000 + (i % 50) * 10,
                quality: int = 30) -> bytes:
    """n_scans voltas completas de pacotes Standard; o primeiro de cada volta tem S=1."""
    out = bytearray()
    for _ in range(n_scans):
        for i in range(points):
            out += encode_measurement(i * 360.0 / points, dist(i), quality, start=(i == 0))
    return bytes(out)


class FakeSerial:
    """Imita o subconjunto de serial.Serial usado pelo driver."""

    def __init__(self, responder=None):
        self.written: list[bytes] = []
        self._rx = bytearray()
        self._lock = threading.Lock()
        self.responder = responder
        self.closed = False
        self.disconnected = False      # simula cabo removido: read/write levantam SerialException
        self.threads: list[threading.Thread] = []

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._rx += data

    def feed_slowly(self, data: bytes, chunk: int = 250, delay: float = 0.002) -> None:
        def run():
            for i in range(0, len(data), chunk):
                if self.closed or self.disconnected:
                    return
                self.feed(data[i:i + chunk])
                time.sleep(delay)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.threads.append(t)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def read(self, n: int = 1) -> bytes:
        if self.disconnected:
            raise serial.SerialException("device reports readiness to read but returned no data")
        with self._lock:
            if self._rx:
                chunk, self._rx = bytes(self._rx[:n]), self._rx[n:]
                return chunk
        time.sleep(0.003)
        return b""

    def write(self, data: bytes) -> int:
        if self.disconnected:
            raise serial.SerialException("WriteFile failed (PermissionError(13, 'Acesso negado.'))")
        self.written.append(bytes(data))
        if self.responder:
            self.responder(bytes(data), self)
        return len(data)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def close(self) -> None:
        self.closed = True


def make_device(health=(0, 0), scan_stream: bytes | None = None, slow: bool = True,
                health_sequence=None, info_descriptor: bytes = DESC_INFO, silent: bool = False):
    """Responder que se comporta como um C1: A5 50 -> info, A5 52 -> saúde, A5 20 -> descritor + fluxo."""
    healths = list(health_sequence) if health_sequence else None

    def responder(req: bytes, fake: FakeSerial) -> None:
        if silent:
            return
        if req == b"\xA5\x50":
            fake.feed(info_descriptor + INFO_DATA)
        elif req == b"\xA5\x52":
            h = healths.pop(0) if healths and len(healths) > 1 else (healths[0] if healths else health)
            fake.feed(DESC_HEALTH + health_data(*h))
        elif req == b"\xA5\x20":
            fake.feed(DESC_SCAN)
            if scan_stream:
                (fake.feed_slowly if slow else fake.feed)(scan_stream)

    return responder
