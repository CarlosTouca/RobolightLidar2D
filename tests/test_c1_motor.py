"""Velocidade do motor do C1: pacotes, medição e o script de bancada (contra serial falsa; NÃO substitui o aparelho)."""
import importlib.util
import struct
from pathlib import Path

import pytest

from backend.sources.base import SourceError
from backend.sources.rplidar_c1 import RplidarC1
from backend.sources.rplidar_protocol import (
    ProtocolError, build_lidar_conf_request, build_motor_speed_request, parse_lidar_conf,
)
from tests.fake_serial import FakeSerial, make_device, make_stream

_spec = importlib.util.spec_from_file_location("c1_motor_test", Path(__file__).parent.parent / "scripts" / "c1_motor_test.py")
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)

STOP = b"\xA5\x25"


def xor(b: bytes) -> int:
    c = 0
    for x in b:
        c ^= x
    return c


def test_motor_speed_request_bytes_and_limits():
    req = build_motor_speed_request(600)
    assert req[:5] == bytes([0xA5, 0xA8, 0x02, 0x58, 0x02]) and req[5] == xor(req[:5])     # [P28] p.32: rpm u16 LE + checksum
    for bad in (479, 721, 0, 5000):
        with pytest.raises(ValueError):
            build_motor_speed_request(bad)


def test_lidar_conf_request_and_response_parsing():
    req = build_lidar_conf_request(0x04)
    assert req[:7] == bytes([0xA5, 0x84, 0x04, 0x04, 0, 0, 0]) and req[7] == xor(req[:7])  # [P28] p.28: tipo u32 LE
    assert parse_lidar_conf(struct.pack("<IH", 4, 480)) == (4, struct.pack("<H", 480))
    with pytest.raises(ProtocolError):
        parse_lidar_conf(b"\x01\x00")


def test_rpm_from_times_and_verdicts():
    assert mt.rpm_from_times([i * 0.1 for i in range(10)]) == pytest.approx(600)
    with pytest.raises(ValueError):
        mt.rpm_from_times([0.0, 0.1])
    ok = [{"alvo_rpm": 540, "medido_rpm": 541}, {"alvo_rpm": 660, "medido_rpm": 655}]
    assert mt.verdict(600, ok).startswith("SEGUIU")
    stuck = [{"alvo_rpm": 540, "medido_rpm": 601}, {"alvo_rpm": 660, "medido_rpm": 598}]
    assert mt.verdict(600, stuck).startswith("IGNOROU")
    assert mt.verdict(600, [ok[0], stuck[1]]).startswith("INCONCLUSIVO")


def test_targets_are_limited_by_datasheet_and_by_what_the_sensor_reports():
    assert mt.check_targets([480, 600, 720]) == [480, 600, 720]
    for bad in ([479], [721], [900]):
        with pytest.raises(ValueError):
            mt.check_targets(bad)
    limits = {"min": {"rpm": 540}, "max": {"rpm": 660}, "desired": {"rpm": 600}}
    assert mt.check_targets([540, 660], limits) == [540, 660]
    with pytest.raises(ValueError):
        mt.check_targets([500], limits)                       # dentro do datasheet, fora do que o sensor disse
    assert mt.check_targets([500], {"min": {"rpm": 0}, "max": {"erro": "sem resposta"}}) == [500]


def test_cli_refuses_to_change_speed_without_confirmation_or_outside_range():
    with pytest.raises(SystemExit) as e:
        mt.main(["--port", "COM9", "--set-rpm", "540"])        # falta --i-understand
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        mt.main(["--port", "COM9", "--set-rpm", "900", "--i-understand"])
    assert e.value.code == 2


def conf_device(respond_conf=True, scan_stream=None):
    base = make_device(scan_stream=scan_stream)
    answers = {1: struct.pack("<HH", 600, 0), 4: struct.pack("<H", 480), 5: struct.pack("<H", 720)}

    def responder(req: bytes, fake: FakeSerial) -> None:
        if req[:2] == b"\xA5\x84" and respond_conf:
            entry = struct.unpack_from("<I", req, 3)[0]
            data = struct.pack("<I", entry) + answers[entry]
            fake.feed(bytes([0xA5, 0x5A]) + struct.pack("<I", len(data)) + b"\x20" + data)
        else:
            base(req, fake)
    return responder


def open_driver(responder):
    fake = FakeSerial(responder)
    drv = RplidarC1(serial_factory=lambda port, **k: fake, info_timeout=0.5, reboot_wait=0.0)
    drv.open("COM9")
    return drv, fake


def scripted(values):
    it = iter(values)

    def measure(drv, seconds):
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v, 40
    return measure


def test_run_read_only_changes_nothing():
    drv, fake = open_driver(conf_device())
    rep = mt.run(drv, "COM9", [], 1.0, 600, measure=scripted([600.2]), sleep=lambda s: None)
    assert rep["limites"]["min"]["rpm"] == 480 and rep["limites"]["max"]["rpm"] == 720 and rep["limites"]["desired"]["rpm"] == 600
    assert rep["rotacao_inicial"]["rpm"] == 600.2 and rep["veredito"].startswith("SÓ LEITURA")
    assert not any(w[:2] == b"\xA5\xA8" for w in fake.written)          # nenhum MOTOR_SPEED_CTRL
    assert fake.written[-1] == STOP


def test_run_changes_speed_measures_and_always_restores():
    drv, fake = open_driver(conf_device())
    rep = mt.run(drv, "COM9", [540, 660], 1.0, 600, measure=scripted([600, 538, 661, 601]), sleep=lambda s: None)
    sent = [struct.unpack_from("<H", w, 3)[0] for w in fake.written if w[:2] == b"\xA5\xA8"]
    assert sent == [540, 660, 600]                                       # os dois pedidos e a restauração
    assert rep["veredito"].startswith("SEGUIU") and rep["restaurado"]["medido_rpm"] == 601
    assert fake.written[-1] == STOP


def test_run_restores_even_when_a_measurement_fails():
    drv, fake = open_driver(conf_device())
    rep = mt.run(drv, "COM9", [540], 1.0, 600, measure=scripted([600, ValueError("poucas voltas"), 600]), sleep=lambda s: None)
    assert rep["mudancas"][0]["medido_rpm"] is None and "poucas voltas" in rep["mudancas"][0]["erro"]
    sent = [struct.unpack_from("<H", w, 3)[0] for w in fake.written if w[:2] == b"\xA5\xA8"]
    assert sent == [540, 600] and fake.written[-1] == STOP


def test_run_tolerates_a_firmware_that_does_not_answer_get_lidar_conf():
    drv, fake = open_driver(conf_device(respond_conf=False))
    drv._info_timeout = 0.15
    rep = mt.run(drv, "COM9", [], 1.0, 600, measure=scripted([600]), sleep=lambda s: None)
    assert all("erro" in rep["limites"][k] for k in ("desired", "min", "max"))   # registra o erro e segue


def test_set_motor_rpm_needs_active_scan_and_valid_range():
    drv, fake = open_driver(conf_device())
    with pytest.raises(SourceError):
        drv.set_motor_rpm(600)                                           # varredura parada: o motor nem gira
    drv.start_scan()
    with pytest.raises(SourceError):
        drv.set_motor_rpm(900)                                           # fora de 480-720
    drv.set_motor_rpm(660)
    assert fake.written[-1][:3] == b"\xA5\xA8\x02"
