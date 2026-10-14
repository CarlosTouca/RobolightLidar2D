"""Parser do protocolo do C1 testado com bytes sintéticos e vetores tirados do manual v2.2."""
import random

import numpy as np
import pytest

from backend.models import HealthStatus
from backend.sources import rplidar_protocol as P
from backend.sources.rplidar_protocol import (
    ProtocolError, StandardScanDecoder, build_request, encode_measurement, find_descriptor,
    packet_is_valid, parse_descriptor, parse_device_info, parse_health,
)
from tests.fake_serial import INFO_DATA, make_stream


# --- pedidos: bytes do manual [P22] p.14-16, p.34, p.36 ------------------------------------
def test_request_bytes_match_manual():
    assert build_request(P.CMD_STOP) == b"\xA5\x25"
    assert build_request(P.CMD_RESET) == b"\xA5\x40"
    assert build_request(P.CMD_SCAN) == b"\xA5\x20"
    assert build_request(P.CMD_GET_INFO) == b"\xA5\x50"
    assert build_request(P.CMD_GET_HEALTH) == b"\xA5\x52"


def test_request_with_payload_has_size_and_xor_checksum():
    # checksum = 0 ^ A5 ^ cmd ^ size ^ payload...  ([P22] p.7), calculado à mão:
    # A5^A8=0D, ^02=0F, ^34=3B, ^02=39
    assert build_request(0xA8, b"\x34\x02") == bytes([0xA5, 0xA8, 0x02, 0x34, 0x02, 0x39])
    with pytest.raises(ValueError):
        build_request(0x01, bytes(256))


# --- descritores ---------------------------------------------------------------------------------
@pytest.mark.parametrize("hexstr,length,mode,dtype", [
    ("A5 5A 05 00 00 40 81", 5, 1, 0x81),     # SCAN   [P22] p.16
    ("A5 5A 14 00 00 00 04", 20, 0, 0x04),    # INFO   [P22] p.34
    ("A5 5A 03 00 00 00 06", 3, 0, 0x06),     # HEALTH [P22] p.36
])
def test_descriptors_from_manual(hexstr, length, mode, dtype):
    d = parse_descriptor(bytes.fromhex(hexstr))
    assert (d.length, d.send_mode, d.data_type) == (length, mode, dtype)


def test_bad_descriptor_rejected():
    with pytest.raises(ProtocolError):
        parse_descriptor(bytes.fromhex("A5 5B 05 00 00 40 81"))
    with pytest.raises(ProtocolError):
        parse_descriptor(b"\xA5\x5A\x05")


def test_find_descriptor_skips_garbage_and_handles_split_arrival():
    buf = bytearray(b"boot text...\xFF\xA5")
    assert find_descriptor(buf) is None                      # A5 pode ser o início do descritor
    buf += bytes.fromhex("5A 05 00")
    assert find_descriptor(buf) is None                      # ainda incompleto
    buf += bytes.fromhex("00 40 81") + b"\x11\x22"
    d = find_descriptor(buf)
    assert d.data_type == 0x81 and d.send_mode == 1
    assert bytes(buf) == b"\x11\x22"                          # dados após o descritor preservados


# --- info e saúde ------------------------------------------------------------------------------------
def test_parse_device_info():
    info = parse_device_info(INFO_DATA)
    assert info.model == "RPLIDAR (ID 0x41)" and info.firmware == "1.03" and info.hardware == "18"
    assert info.serial == "000102030405060708090A0B0C0D0E0F"     # serialnumber[0] primeiro
    with pytest.raises(ProtocolError):
        parse_device_info(INFO_DATA[:-1])


@pytest.mark.parametrize("raw,status,code", [
    (b"\x00\x00\x00", HealthStatus.OK, 0),
    (b"\x01\x34\x12", HealthStatus.WARNING, 0x1234),         # error_code little-endian
    (b"\x02\xFF\x00", HealthStatus.ERROR, 255),
])
def test_parse_health(raw, status, code):
    h = parse_health(raw)
    assert h.status is status and h.error_code == code


def test_parse_health_rejects_unknown_status_or_size():
    with pytest.raises(ProtocolError):
        parse_health(b"\x07\x00\x00")
    with pytest.raises(ProtocolError):
        parse_health(b"\x00\x00")


# --- pacote de medida: vetor calculado à mão a partir da Fig. 4-4/4-5 -----------------------------
def test_measurement_packet_hand_computed_vector():
    # qualidade 40, S=1, ângulo 90,0° (angle_q6 = 5760 = 0x1680), distância 1000 mm (q2 = 4000 = 0x0FA0)
    #  +0: 40<<2 | S̅<<1 | S = 0xA0 | 0 | 1            = 0xA1
    #  +1: angle_q6[6:0]<<1 | C = (0x00 << 1) | 1        = 0x01
    #  +2: angle_q6[14:7] = 0x1680 >> 7                  = 0x2D
    #  +3/+4: distância LE                               = A0 0F
    raw = bytes([0xA1, 0x01, 0x2D, 0xA0, 0x0F])
    assert encode_measurement(90.0, 1000.0, 40, start=True) == raw
    dec = StandardScanDecoder()
    stream = raw + make_stream(1, points=60) + make_stream(1, points=60)   # 3 voltas
    scans = dec.feed(stream)
    first = scans[0]
    assert first.angle_deg[0] == pytest.approx(90.0)
    assert first.distance_mm[0] == pytest.approx(1000.0)
    assert first.quality[0] == 40


def test_validity_bits():
    ok = encode_measurement(10.0, 500.0, 20, start=False)
    assert packet_is_valid(ok)
    bad_sbar = bytes([ok[0] | 0b11]) + ok[1:]                  # S == S̅
    bad_sbar2 = bytes([ok[0] & ~0b11]) + ok[1:]                # S == S̅ == 0
    bad_c = bytes([ok[0], ok[1] & 0xFE]) + ok[2:]              # C == 0
    bad_angle = bytes([ok[0], ok[1], 0xFF]) + ok[3:]           # angle_q6 >= 360*64
    assert not any(packet_is_valid(p) for p in (bad_sbar, bad_sbar2, bad_c, bad_angle))


# --- decodificador de voltas ---------------------------------------------------------------------------
def chunks(data: bytes, rng: random.Random):
    i = 0
    while i < len(data):
        n = rng.randint(1, 97)
        yield data[i:i + n]
        i += n


def test_decoder_emits_complete_scans_with_values_regardless_of_chunking():
    stream = make_stream(4, points=500)
    dec = StandardScanDecoder()
    scans = []
    for c in chunks(stream, random.Random(1)):
        scans += dec.feed(c)
    assert len(scans) == 3                                     # a 4ª só fecha no próximo S=1
    assert dec.errors == 0 and dec.discarded_bytes == 0
    for s in scans:
        assert len(s.angle_deg) == 500
        assert s.angle_deg[0] == 0 and s.angle_deg[250] == pytest.approx(180.0, abs=0.02)
        assert s.distance_mm[7] == 2000 + 7 * 10
        assert (s.quality == 30).all()
        assert (np.diff(s.angle_deg) > 0).all() and s.angle_deg.max() < 360


def test_points_before_first_start_flag_are_ignored():
    partial = b"".join(encode_measurement(300 + i * 0.7, 1500, 20, start=False) for i in range(40))
    dec = StandardScanDecoder()
    scans = dec.feed(partial + make_stream(3, points=100))
    assert len(scans) == 2 and all(len(s.angle_deg) == 100 for s in scans)
    assert dec.errors == 0


def test_invalid_distance_zero_is_kept_for_the_pipeline_to_drop():
    stream = bytearray(make_stream(3, points=100))
    stream[5 * 10:5 * 10 + 5] = encode_measurement(7.2, 0.0, 0, start=False)
    scans = StandardScanDecoder().feed(bytes(stream))
    assert scans[0].distance_mm[10] == 0 and scans[0].quality[10] == 0
    from backend.pipeline import scan_to_frame
    assert len(scan_to_frame(scans[0]).x) == 99


def test_corrupted_packet_is_discarded_counted_and_stream_recovers():
    pts = 200
    stream = bytearray(make_stream(6, points=pts))     # 6ª volta, íntegra, fecha a 5ª
    # corrompe, em voltas diferentes, um pacote de cada tipo de erro
    def kill(scan_idx, point_idx, how):
        o = (scan_idx * pts + point_idx) * 5
        if how == "sbar":
            stream[o] |= 0b11
        elif how == "c":
            stream[o + 1] &= 0xFE
        elif how == "angle":
            stream[o + 2] = 0xFF
    kill(1, 50, "sbar"); kill(2, 80, "c"); kill(3, 120, "angle")
    dec = StandardScanDecoder()
    scans = []
    for c in chunks(bytes(stream), random.Random(2)):
        scans += dec.feed(c)
    assert dec.errors >= 3                                    # erros contados (um por trecho)
    assert dec.discarded_bytes >= 3
    assert len(scans) >= 3                                    # voltas continuam saindo
    for s in scans:
        assert s.angle_deg.min() >= 0 and s.angle_deg.max() < 360   # nada de ângulo corrompido passa
    assert len(scans[-1].angle_deg) == pts                    # volta depois da última corrupção: íntegra


def test_garbage_run_counts_as_one_error_and_resynchronizes():
    stream = make_stream(3, points=100)
    junk = bytes([0x00, 0x13, 0xFE, 0x77, 0x00, 0x42, 0x01])   # 7 bytes (não múltiplo de 5)
    cut = 5 * 50
    dec = StandardScanDecoder()
    scans = dec.feed(stream[:cut] + junk + stream[cut:])
    # Os bits de verificação do modo Standard são fracos: parte do lixo pode passar por um pacote
    # válido (limitação do protocolo). O essencial: o trecho conta UM erro e o alinhamento volta.
    assert dec.errors == 1 and dec.discarded_bytes >= 1
    assert len(scans) >= 1 and len(scans[-1].angle_deg) in (100, 101, 99, 98)
    scans2 = dec.feed(make_stream(2, points=100))              # e a volta seguinte vem íntegra
    assert all(len(s.angle_deg) == 100 for s in scans2[1:])


def test_decoder_never_raises_on_random_bytes_and_emits_only_valid_angles():
    rng = random.Random(3)
    dec = StandardScanDecoder()
    for _ in range(300):
        for s in dec.feed(bytes(rng.randrange(256) for _ in range(rng.randint(1, 300)))):
            assert s.angle_deg.min() >= 0 and s.angle_deg.max() < 360


def test_missing_start_flag_does_not_grow_without_bound():
    # uma volta ok e depois 5000 pacotes S=0: o limite por volta dispara e conta erro
    stream = make_stream(2, points=50)[:5 * 50 + 5]
    flood = b"".join(encode_measurement((i * 0.7) % 360, 1000, 10, start=False) for i in range(5000))
    dec = StandardScanDecoder()
    dec.feed(stream + flood)
    assert dec.errors >= 1
    assert len(dec._angles) <= P.MAX_POINTS_PER_SCAN + 1
