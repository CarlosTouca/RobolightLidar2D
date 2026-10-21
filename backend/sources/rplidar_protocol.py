"""Protocolo serial do RPLIDAR C1 — parser e codificadores, sem I/O.

FONTES (lidas diretamente; ver README, seção "Validação do protocolo"):
  [DS]  "RPLIDAR C1 Introduction and Datasheet", rev.1.0 (2023-10-13), modelo C1M1-R2.
        §Protocol Compatibility (p.7): o C1 é compatível com os protocolos da série S e com
        o protocolo tradicional ("standard") da série A.
  [P22] "LR001 SLAMTEC rplidar protocol v2.2" (2021-02-25): protocolo das séries A e S.
        As páginas citadas são as do PDF. O documento específico do C1/S&C (v2.8) é hospedado
        pela Slamtec mas devolveu HTTP 403 a este ambiente: NÃO foi lido. Tudo abaixo vale
        para o modo Standard (SCAN) e para os pedidos básicos, e DEVE ser confirmado no
        aparelho (pendente de validação com o hardware).

Convenção do projeto (backend/models.py): 0° = frente, sentido horário. O datasheet [DS]
(§Coordinate System Definition, p.11) diz o mesmo: eixo x à frente, ângulo cresce no sentido
horário; [P22] §4.4 Fig. 4-6: θ ∈ [0, 360).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from ..models import DeviceInfo, Health, HealthStatus, Scan

# --- Pedidos ([P22] §2.3 "Request Packets' Format", p.6-7; Fig. 2-4) ---------------------
# Formato: 0xA5, comando, [tamanho do payload, payload, checksum] (as 3 partes entre
# colchetes só existem se o pedido tem payload). Checksum = XOR de todos os bytes anteriores.
REQ_START = 0xA5
CMD_STOP = 0x25        # [P22] §4.2 p.14: sem resposta; esperar >= 1 ms
CMD_RESET = 0x40       # [P22] §4.3 p.15: sem resposta; esperar >= 2 ms
CMD_SCAN = 0x20        # [P22] §4.4 p.15-16: modo Legacy/Standard
CMD_GET_INFO = 0x50    # [P22] §4.8 p.34
CMD_GET_HEALTH = 0x52  # [P22] §4.9 p.36
# EXPERIMENTAIS: usados só por scripts/c1_motor_test.py; NÃO validados no C1 real (ver README, "Velocidade do motor").
CMD_GET_LIDAR_CONF = 0x84      # [P28] §4 p.28-29: A5 84 04 <tipo u32> <checksum>; resposta descritor tipo 0x20, dados = tipo u32 + valor
CMD_MOTOR_SPEED_CTRL = 0xA8    # [P28] §4 p.32: A5 A8 02 <rpm u16> <checksum>; rpm = 0 leva ao estado ocioso
TYPE_LIDAR_CONF = 0x20         # tipo do descritor de resposta do GET_LIDAR_CONF (SDK: SL_LIDAR_ANS_TYPE_GET_LIDAR_CONF)
# Entradas de rotação: só existem nos cabeçalhos do SDK (sl_lidar_cmd.h); o protocolo v2.8 documenta apenas 0x70-0x7F.
CONF_DESIRED_ROT_FREQ, CONF_MIN_ROT_FREQ, CONF_MAX_ROT_FREQ = 0x01, 0x04, 0x05
C1_MIN_RPM, C1_MAX_RPM = 480, 720   # datasheet C1 rev.1.1 p.9: 8~12 Hz (10 Hz típico)

STOP_WAIT_S = 0.001    # [P22] p.14: "at least 1 millisecond"
RESET_WAIT_S = 0.002   # [P22] p.15: "at least 2 milliseconds"

# [P28] = "LR001 SLAMTEC rplidar protocol v2.8" (S & C series, 2023-10-16), lido em 2026-10-20.

# --- Descritor de resposta ([P22] §2.4 "Response Packets' Format", p.8-9; Figs. 2-7, 2-8) --
# 0xA5 0x5A, depois 4 bytes little-endian: bits 0..29 = tamanho de cada pacote de dados,
# bits 30..31 = modo de envio (0 = uma resposta, 1 = respostas contínuas); depois 1 byte de tipo.
ANS_SYNC1 = 0xA5
ANS_SYNC2 = 0x5A
DESCRIPTOR_SIZE = 7
SEND_SINGLE = 0x0
SEND_MULTIPLE = 0x1
TYPE_DEVINFO = 0x04       # descritor A5 5A 14 00 00 00 04 ([P22] p.34, Fig. 4-27)
TYPE_HEALTH = 0x06        # descritor A5 5A 03 00 00 00 06 ([P22] p.36)
TYPE_MEASUREMENT = 0x81   # descritor A5 5A 05 00 00 40 81 ([P22] p.16, Fig. 4-4)

DEVINFO_SIZE = 20         # [P22] p.34 Fig. 4-25
HEALTH_SIZE = 3           # [P22] p.36 Fig. 4-28
MEASUREMENT_SIZE = 5      # [P22] p.16 Fig. 4-4

# --- Pacote de medida do modo Standard ([P22] §4.4 p.16, Fig. 4-4/4-5) ---------------------
#   +0: bits 7..2 = qualidade, bit 1 = S̅ (S invertido), bit 0 = S (início de nova volta)
#   +1: bits 7..1 = angle_q6[6:0], bit 0 = C (sempre 1)
#   +2: angle_q6[14:7]
#   +3: distance_q2[7:0]      +4: distance_q2[15:8]
#   ângulo = angle_q6 / 64.0 graus; distância = distance_q2 / 4.0 mm; 0 = medida inválida.
# Bits de verificação ([P22] Fig. 4-5): "S̅ = !S" e "C constantemente 1".
ANGLE_Q6_FULL = 360 * 64   # ângulo válido: angle_q6 < 23040  ([0, 360) graus)

LOCK_PACKETS = 3             # pacotes válidos seguidos para confirmar o alinhamento
MAX_POINTS_PER_SCAN = 4000  # 5000 amostras/s a 8 Hz ≈ 625; acima disso o sincronismo se perdeu

HEALTH_BY_CODE = {0: HealthStatus.OK, 1: HealthStatus.WARNING, 2: HealthStatus.ERROR}  # [P22] Fig. 4-29


class ProtocolError(Exception):
    """Dados do dispositivo que violam o protocolo (descritor inesperado, tamanho errado...)."""


# --- Pedidos -------------------------------------------------------------------------------
def build_request(cmd: int, payload: bytes = b"") -> bytes:
    if not payload:
        return bytes([REQ_START, cmd])
    if len(payload) > 255:
        raise ValueError("payload de no máximo 255 bytes")
    body = bytes([REQ_START, cmd, len(payload)]) + payload
    checksum = 0
    for b in body:
        checksum ^= b
    return body + bytes([checksum])


# --- Descritor ---------------------------------------------------------------------------------
@dataclass(frozen=True)
class Descriptor:
    length: int       # tamanho de cada pacote de dados, em bytes
    send_mode: int    # 0 = única, 1 = múltiplas
    data_type: int


def parse_descriptor(raw: bytes) -> Descriptor:
    if len(raw) != DESCRIPTOR_SIZE or raw[0] != ANS_SYNC1 or raw[1] != ANS_SYNC2:
        raise ProtocolError(f"descritor inválido: {raw.hex(' ')}")
    (word,) = struct.unpack_from("<I", raw, 2)
    return Descriptor(length=word & 0x3FFFFFFF, send_mode=word >> 30, data_type=raw[6])


def find_descriptor(buf: bytearray) -> Descriptor | None:
    """Procura A5 5A no buffer (descartando lixo anterior, p.ex. texto de boot após RESET).

    Devolve o descritor e remove-o (com o lixo) do buffer; None se ainda não há 7 bytes.
    """
    while True:
        i = buf.find(bytes([ANS_SYNC1, ANS_SYNC2]))
        if i < 0:
            del buf[:-1]          # mantém o último byte: pode ser o A5 de um descritor em chegada
            return None
        del buf[:i]
        if len(buf) < DESCRIPTOR_SIZE:
            return None
        desc = parse_descriptor(bytes(buf[:DESCRIPTOR_SIZE]))
        del buf[:DESCRIPTOR_SIZE]
        return desc


def expect_descriptor(desc: Descriptor, length: int, send_mode: int, data_type: int, what: str) -> None:
    if (desc.length, desc.send_mode, desc.data_type) != (length, send_mode, data_type):
        raise ProtocolError(
            f"resposta inesperada para {what}: tamanho={desc.length} modo={desc.send_mode} "
            f"tipo=0x{desc.data_type:02X} (esperado {length}/{send_mode}/0x{data_type:02X})")


# --- Velocidade do motor (EXPERIMENTAL) -------------------------------------------------------
def build_lidar_conf_request(entry: int) -> bytes:
    return build_request(CMD_GET_LIDAR_CONF, struct.pack("<I", entry))


def build_motor_speed_request(rpm: int) -> bytes:
    """[P28] p.32. Recusa fora de 480-720 rpm (8-12 Hz, datasheet do C1); rpm = 0 (ocioso) tem função própria no STOP."""
    if not C1_MIN_RPM <= rpm <= C1_MAX_RPM:
        raise ValueError(f"rpm {rpm} fora da faixa do C1 ({C1_MIN_RPM}-{C1_MAX_RPM}, datasheet p.9)")
    return build_request(CMD_MOTOR_SPEED_CTRL, struct.pack("<H", rpm))


def parse_lidar_conf(data: bytes) -> tuple[int, bytes]:
    """Resposta do GET_LIDAR_CONF: (tipo, valor bruto). O valor de cada entrada varia ([P28] p.29)."""
    if len(data) < 4:
        raise ProtocolError(f"GET_LIDAR_CONF: {len(data)} bytes (esperado >= 4)")
    return struct.unpack_from("<I", data, 0)[0], bytes(data[4:])


# --- GET_INFO / GET_HEALTH ----------------------------------------------------------------------
def parse_device_info(data: bytes) -> DeviceInfo:
    """[P22] p.34-35, Figs. 4-25/4-26: model, firmware_minor, firmware_major, hardware, serial[16].

    O manual lido (v2.2) não traz a tabela do ID de modelo; ele é exibido em hexadecimal, sem
    inferir o nome comercial. Número de série em hex, byte menos significativo primeiro (Fig. 4-26).
    """
    if len(data) != DEVINFO_SIZE:
        raise ProtocolError(f"GET_INFO: {len(data)} bytes (esperado {DEVINFO_SIZE})")
    model, fw_minor, fw_major, hardware = data[0], data[1], data[2], data[3]
    return DeviceInfo(model=f"RPLIDAR (ID 0x{model:02X})",
                      firmware=f"{fw_major}.{fw_minor:02d}",
                      hardware=str(hardware),
                      serial=data[4:20].hex().upper())


def parse_health(data: bytes) -> Health:
    """[P22] p.36, Figs. 4-28/4-29: status (0 bom, 1 aviso, 2 erro) + error_code (16 bits LE)."""
    if len(data) != HEALTH_SIZE:
        raise ProtocolError(f"GET_HEALTH: {len(data)} bytes (esperado {HEALTH_SIZE})")
    status = HEALTH_BY_CODE.get(data[0])
    if status is None:
        raise ProtocolError(f"GET_HEALTH: status desconhecido {data[0]}")
    return Health(status, struct.unpack_from("<H", data, 1)[0])


# --- Medidas (modo Standard) --------------------------------------------------------------------
def encode_measurement(angle_deg: float, distance_mm: float, quality: int, start: bool) -> bytes:
    """Gera um pacote de 5 bytes (para testes sintéticos e simulação de bytes brutos)."""
    angle_q6 = int(round(angle_deg * 64)) & 0x7FFF
    dist_q2 = int(round(distance_mm * 4)) & 0xFFFF
    s = 1 if start else 0
    b0 = ((quality & 0x3F) << 2) | ((1 - s) << 1) | s
    return bytes([b0, ((angle_q6 & 0x7F) << 1) | 1, angle_q6 >> 7, dist_q2 & 0xFF, dist_q2 >> 8])


def packet_is_valid(p: bytes | bytearray) -> bool:
    """Verificações de [P22] Fig. 4-5: S̅ == !S, C == 1; e ângulo dentro de [0, 360)."""
    s, s_bar = p[0] & 1, (p[0] >> 1) & 1
    if s == s_bar or not (p[1] & 1):
        return False
    angle_q6 = (p[2] << 7) | (p[1] >> 1)
    return angle_q6 < ANGLE_Q6_FULL


@dataclass
class StandardScanDecoder:
    """Decodificador incremental do fluxo de medidas do modo Standard.

    * Valida cada pacote de 5 bytes (S/S̅, C e faixa do ângulo). Se falha, descarta 1 byte e
      ressincroniza: como os bits de verificação são poucos (um alinhamento errado passa em
      ~17% das vezes), o alinhamento só é aceito de novo após LOCK_PACKETS pacotes válidos
      seguidos. Cada trecho contíguo de bytes descartados conta como UM erro em `errors`;
      `discarded_bytes` conta os bytes.
    * Uma volta só é emitida quando o próximo S=1 chega (voltas completas); os pontos
      anteriores ao primeiro S=1 pertencem a uma volta incompleta e são ignorados.
    """
    errors: int = 0
    discarded_bytes: int = 0
    scans: int = 0
    _buf: bytearray = field(default_factory=bytearray)
    _angles: list[float] = field(default_factory=list)
    _dists: list[float] = field(default_factory=list)
    _quals: list[int] = field(default_factory=list)
    _synced: bool = False         # já vimos o primeiro S=1?
    _aligned: bool = False        # alinhamento de pacotes confirmado
    _in_garbage: bool = False

    def feed(self, data: bytes) -> list[Scan]:
        self._buf += data
        out: list[Scan] = []
        buf = self._buf
        pos = 0
        n = len(buf)
        while True:
            need = MEASUREMENT_SIZE if self._aligned else LOCK_PACKETS * MEASUREMENT_SIZE
            if n - pos < need:
                break
            if self._aligned:
                ok = packet_is_valid(buf[pos:pos + MEASUREMENT_SIZE])
            else:
                ok = all(packet_is_valid(buf[pos + k * MEASUREMENT_SIZE:pos + (k + 1) * MEASUREMENT_SIZE])
                         for k in range(LOCK_PACKETS))
            if not ok:
                self._aligned = False
                if not self._in_garbage:
                    self.errors += 1
                    self._in_garbage = True
                self.discarded_bytes += 1
                pos += 1
                continue
            self._aligned = True
            self._in_garbage = False
            scan = self._accept(buf[pos:pos + MEASUREMENT_SIZE])
            pos += MEASUREMENT_SIZE
            if scan is not None:
                out.append(scan)
        del buf[:pos]
        return out

    def _accept(self, p: bytes | bytearray) -> Scan | None:
        finished = None
        if p[0] & 1:                                   # S = 1: começa uma nova volta
            if self._synced and self._angles:
                finished = Scan(angle_deg=np.array(self._angles, float),
                                distance_mm=np.array(self._dists, float),
                                quality=np.array(self._quals, int))
                self.scans += 1
            self._synced = True
            self._angles, self._dists, self._quals = [], [], []
        if not self._synced:
            return None
        self._angles.append(((p[2] << 7) | (p[1] >> 1)) / 64.0)
        self._dists.append(((p[4] << 8) | p[3]) / 4.0)
        self._quals.append(p[0] >> 2)
        if len(self._angles) > MAX_POINTS_PER_SCAN:    # S=1 não chegou: sincronismo perdido
            self.errors += 1
            self._synced = False
            self._angles, self._dists, self._quals = [], [], []
        return finished
