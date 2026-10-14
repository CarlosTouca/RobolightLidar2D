"""Driver real do RPLIDAR C1 sobre pyserial (460800 baud, 8N1).

PENDENTE DE VALIDAÇÃO COM O HARDWARE: o parser e o driver foram testados só com bytes
sintéticos e uma serial falsa. Fontes e ressalvas em backend/sources/rplidar_protocol.py.

Motor (verificado nos documentos do C1; ver README):
  * [DS] datasheet C1, §Scanner Motor Control (p.14): "the motor can't start or stop alone, and
    its working state depends on the laser scan command". Não há pino de controle do motor
    (a tabela de pinos, Fig. 2-6, só tem VCC, TX, RX, GND; nada de DTR/PWM/MOTOCTL).
  * Logo: SCAN (A5 20) liga o motor e STOP (A5 25) o desliga ([P22] §4.2: STOP -> estado Idle,
    "laser diode and the measurement system will be disabled").
  * O ajuste de velocidade existe por comando, mas o comando MOTOR_SPEED_CTRL do manual v2.2
    (§4.11 p.44) é só para a série S1, e o manual do C1 (v2.8) não pôde ser lido: NÃO é usado.

Sequência ([P22] §5.1 Fig. 5-1): GET_HEALTH antes de SCAN; em Protection Stop, RESET.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator

from ..config import BAUDRATE
from ..models import DeviceInfo, Health, HealthStatus, Scan
from .base import LidarSource, SourceError
from .rplidar_protocol import (
    CMD_GET_HEALTH, CMD_GET_INFO, CMD_RESET, CMD_SCAN, CMD_STOP, DEVINFO_SIZE, HEALTH_SIZE,
    MEASUREMENT_SIZE, RESET_WAIT_S, SEND_MULTIPLE, SEND_SINGLE, STOP_WAIT_S, TYPE_DEVINFO,
    TYPE_HEALTH, TYPE_MEASUREMENT, ProtocolError, StandardScanDecoder, build_request,
    expect_descriptor, find_descriptor, parse_device_info, parse_health,
)

try:
    import serial
except ImportError:    # pragma: no cover
    serial = None

SerialFactory = Callable[..., "serial.Serial"]

REBOOT_WAIT_S = 0.5    # espera após RESET (o manual só exige >= 2 ms; reinício completo é mais lento)


def _default_factory(port: str, **kw):
    if serial is None:
        raise SourceError("pyserial não está instalado")
    return serial.Serial(port, **kw)


def translate_open_error(port: str, err: BaseException) -> str:
    """Mensagem clara em português para falhas ao abrir a porta."""
    text = str(err)
    low = text.lower()
    if (isinstance(err, PermissionError) or "permissionerror" in low or "(13," in low
            or "acesso negado" in low or "access is denied" in low):
        return (f"porta {port} ocupada ou sem permissão: feche outros programas que a usem "
                f"(RoboStudio, outro terminal, outra aba) e tente de novo")
    if (isinstance(err, FileNotFoundError) or "filenotfounderror" in low or "(2," in low
            or "could not open port" in low or "não pode encontrar" in low):
        return f"porta {port} não encontrada: o cabo/adaptador USB do sensor está conectado?"
    return f"não foi possível abrir {port}: {text}"


class RplidarC1(LidarSource):
    name = "rplidar_c1"

    def __init__(self, serial_factory: SerialFactory | None = None, *, info_timeout: float = 2.0,
                 first_data_timeout: float = 5.0, data_timeout: float = 2.0,
                 reboot_wait: float = REBOOT_WAIT_S):
        self._factory = serial_factory or _default_factory
        self._info_timeout = info_timeout
        self._first_data_timeout = first_data_timeout   # o motor precisa estabilizar ([P22] p.16)
        self._data_timeout = data_timeout
        self._ser = None
        self._port = ""
        self._info: DeviceInfo | None = None
        self._health: Health | None = None
        self._scanning = threading.Event()
        self._decoder = StandardScanDecoder()
        self._errors_base = 0
        self._leftover = b""
        self._reboot_wait = reboot_wait

    # --- portas ----------------------------------------------------------------------------
    @staticmethod
    def list_ports() -> list[str]:
        from .factory import list_serial_ports
        return [p.id for p in list_serial_ports()]

    @property
    def error_count(self) -> int:
        """Pacotes inválidos descartados (bits de verificação, ângulo, sincronismo)."""
        return self._errors_base + self._decoder.errors

    # --- abrir / fechar ----------------------------------------------------------------------
    def open(self, port: str) -> None:
        try:
            ser = self._factory(port, baudrate=BAUDRATE, bytesize=8, parity="N", stopbits=1,
                                timeout=0.1, write_timeout=1.0)
        except (OSError, ValueError) as e:        # SerialException herda de OSError
            raise SourceError(translate_open_error(port, e)) from e
        self._ser, self._port = ser, port
        try:
            self._handshake()
        except Exception:
            self.close()
            raise

    def _handshake(self) -> None:
        """Estado conhecido (STOP), limpa o que sobrou no buffer, lê modelo e saúde."""
        self._write(build_request(CMD_STOP))
        time.sleep(STOP_WAIT_S)
        self._ser.reset_input_buffer()
        self._info = parse_device_info(self._request(CMD_GET_INFO, DEVINFO_SIZE, TYPE_DEVINFO, "GET_INFO"))
        self._health = parse_health(self._request(CMD_GET_HEALTH, HEALTH_SIZE, TYPE_HEALTH, "GET_HEALTH"))

    def close(self) -> None:
        try:
            self.stop_scan()
        finally:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    # --- consultas -----------------------------------------------------------------------------
    def get_info(self) -> DeviceInfo:
        self._require_open()
        return self._info

    def get_health(self) -> Health:
        self._require_open()
        if not self._scanning.is_set():     # com a varredura ativa o fluxo ocupa a linha
            self._health = parse_health(
                self._request(CMD_GET_HEALTH, HEALTH_SIZE, TYPE_HEALTH, "GET_HEALTH"))
        return self._health

    # --- varredura ---------------------------------------------------------------------------------
    def start_scan(self) -> None:
        self._require_open()
        if self._scanning.is_set():
            return
        health = self.get_health()
        if health.status is HealthStatus.ERROR:          # Protection Stop: RESET ([P22] p.15, p.37)
            self._write(build_request(CMD_RESET))
            time.sleep(RESET_WAIT_S + self._reboot_wait)
            self._ser.reset_input_buffer()
            health = self.get_health()
            if health.status is HealthStatus.ERROR:
                raise SourceError(
                    f"sensor em Protection Stop (código de erro {health.error_code}) e o RESET não "
                    f"resolveu: verifique alimentação de 5 V e se o motor gira livremente")
        self._ser.reset_input_buffer()
        self._write(build_request(CMD_SCAN))
        buf = bytearray()
        desc = self._read_descriptor(buf, self._info_timeout, "SCAN")
        try:
            expect_descriptor(desc, MEASUREMENT_SIZE, SEND_MULTIPLE, TYPE_MEASUREMENT, "SCAN")
        except ProtocolError as e:
            raise SourceError(str(e)) from e
        self._errors_base += self._decoder.errors
        self._decoder = StandardScanDecoder()
        self._leftover = bytes(buf)     # medidas que chegaram junto com o descritor
        self._scanning.set()

    def stop_scan(self) -> None:
        was = self._scanning.is_set()
        self._scanning.clear()
        if self._ser is not None and was:
            try:
                self._write(build_request(CMD_STOP))      # STOP -> Idle; o motor para ([DS] p.14)
                time.sleep(STOP_WAIT_S)
            except SourceError:
                pass

    def scans(self) -> Iterator[Scan]:
        ser = self._require_open()
        pending, self._leftover = self._leftover, b""
        last_data = time.monotonic()
        got_any = False
        try:
            if pending:
                yield from self._decode(pending)
            while self._scanning.is_set():
                try:
                    data = ser.read(ser.in_waiting or 1)
                except (OSError, ValueError) as e:
                    raise SourceError("conexão serial perdida: cabo ou adaptador USB desconectado?") from e
                now = time.monotonic()
                if not data:
                    limit = self._data_timeout if got_any else self._first_data_timeout
                    if now - last_data > limit:
                        raise SourceError(
                            f"timeout de leitura: sem dados do sensor há {limit:.0f} s "
                            f"(motor parado, falta de alimentação ou cabo solto?)")
                    continue
                got_any = True
                last_data = now
                yield from self._decode(data)
        except SourceError:
            self._scanning.clear()
            try:
                self._write(build_request(CMD_STOP))
            except Exception:
                pass
            raise

    def _decode(self, data: bytes) -> Iterator[Scan]:
        yield from self._decoder.feed(data)

    # --- baixo nível ------------------------------------------------------------------------------------
    def _require_open(self):
        if self._ser is None:
            raise SourceError("driver não está aberto")
        return self._ser

    def _write(self, data: bytes) -> None:
        ser = self._require_open()
        try:
            ser.write(data)
            ser.flush()
        except (OSError, ValueError) as e:
            raise SourceError("falha ao escrever na porta serial: cabo desconectado ou porta fechada?") from e

    def _read_descriptor(self, buf: bytearray, timeout: float, what: str):
        ser = self._require_open()
        deadline = time.monotonic() + timeout
        while True:
            try:
                buf += ser.read(ser.in_waiting or 1)
            except (OSError, ValueError) as e:
                raise SourceError("conexão serial perdida: cabo ou adaptador USB desconectado?") from e
            try:
                desc = find_descriptor(buf)
            except ProtocolError as e:
                raise SourceError(str(e)) from e
            if desc is not None:
                return desc
            if time.monotonic() > deadline:
                raise SourceError(
                    f"sem resposta ao pedido {what} em {self._port} ({timeout:.0f} s): não parece ser "
                    f"um RPLIDAR C1, a velocidade (460800) está errada ou TX/RX estão invertidos")

    def _request(self, cmd: int, length: int, data_type: int, what: str) -> bytes:
        """Pedido de resposta única: descritor + `length` bytes de dados."""
        self._write(build_request(cmd))
        buf = bytearray()
        desc = self._read_descriptor(buf, self._info_timeout, what)
        try:
            expect_descriptor(desc, length, SEND_SINGLE, data_type, what)
        except ProtocolError as e:
            raise SourceError(str(e)) from e
        ser = self._require_open()
        deadline = time.monotonic() + self._info_timeout
        while len(buf) < length:
            if time.monotonic() > deadline:
                raise SourceError(f"resposta incompleta ao pedido {what}: {len(buf)} de {length} bytes")
            try:
                buf += ser.read(length - len(buf))
            except (OSError, ValueError) as e:
                raise SourceError("conexão serial perdida: cabo ou adaptador USB desconectado?") from e
        return bytes(buf[:length])
