"""Driver do C1 contra uma serial falsa (bytes sintéticos). NÃO substitui o teste no aparelho."""
import time

import pytest
import serial
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.models import HealthStatus
from backend.sources.base import SourceError
from backend.sources.rplidar_c1 import RplidarC1, translate_open_error
from backend.sources.rplidar_protocol import encode_measurement
from tests.fake_serial import DESC_SCAN, FakeSerial, make_device, make_stream

STOP, INFO, HEALTH, SCAN, RESET = b"\xA5\x25", b"\xA5\x50", b"\xA5\x52", b"\xA5\x20", b"\xA5\x40"


def driver(responder, **kw):
    fake = FakeSerial(responder)
    kw.setdefault("info_timeout", 0.5)
    kw.setdefault("reboot_wait", 0.0)
    return RplidarC1(serial_factory=lambda port, **k: fake, **kw), fake


def test_open_handshake_sequence_and_device_info():
    drv, fake = driver(make_device())
    drv.open("COM9")
    assert fake.written == [STOP, INFO, HEALTH]          # STOP -> GET_INFO -> GET_HEALTH
    info = drv.get_info()
    assert info.firmware == "1.03" and info.model == "RPLIDAR (ID 0x41)" and info.serial.startswith("0001")
    assert drv.get_health().status is HealthStatus.OK
    drv.close()
    assert fake.closed


def test_scan_session_streams_scans_and_stop_sends_stop():
    drv, fake = driver(make_device(scan_stream=make_stream(6)))
    drv.open("COM9")
    drv.start_scan()
    assert fake.written[-2:] == [HEALTH, SCAN]            # saúde antes de SCAN ([P22] Fig. 5-1)
    got = []
    for scan in drv.scans():
        got.append(scan)
        if len(got) == 3:
            drv.stop_scan()
    assert len(got) == 3 and all(len(s.angle_deg) == 500 for s in got)
    assert fake.written[-1] == STOP                      # STOP: o motor para
    assert drv.error_count == 0
    drv.close()


def test_corrupted_packets_are_counted_in_error_count():
    stream = bytearray(make_stream(5))
    stream[5 * 100] |= 0b11                              # S == S̅ em um pacote da 1ª volta
    stream[5 * 700 + 1] &= 0xFE                          # C == 0 na 2ª volta
    drv, fake = driver(make_device(scan_stream=bytes(stream)))
    drv.open("COM9")
    drv.start_scan()
    n = 0
    for scan in drv.scans():
        n += 1
        if n == 3:
            drv.stop_scan()
    assert drv.error_count >= 2
    drv.close()


def test_no_response_at_open_is_clear_error_and_closes_port():
    drv, fake = driver(make_device(silent=True), info_timeout=0.2)
    with pytest.raises(SourceError) as e:
        drv.open("COM3")
    assert "sem resposta" in str(e.value) and "COM3" in str(e.value) and "460800" in str(e.value)
    assert fake.closed


def test_wrong_descriptor_is_rejected():
    bad = bytes.fromhex("A5 5A 14 00 00 00 06")            # tipo errado para GET_INFO
    drv, fake = driver(make_device(info_descriptor=bad))
    with pytest.raises(SourceError) as e:
        drv.open("COM9")
    assert "inesperada" in str(e.value)


def test_read_timeout_during_scan_raises_clear_error_and_does_not_hang():
    short = make_stream(2)                               # o fluxo para: motor travou, fio solto...
    drv, fake = driver(make_device(scan_stream=short, slow=False), data_timeout=0.3, first_data_timeout=0.5)
    drv.open("COM9")
    drv.start_scan()
    t0 = time.monotonic()
    with pytest.raises(SourceError) as e:
        for _ in drv.scans():
            pass
    assert "timeout de leitura" in str(e.value)
    assert time.monotonic() - t0 < 2.0
    assert fake.written[-1] == STOP                      # tentou parar o sensor
    drv.close()


def test_no_data_at_all_after_scan_request_uses_first_data_timeout():
    drv, fake = driver(make_device(scan_stream=None), first_data_timeout=0.3)
    drv.open("COM9")
    drv.start_scan()
    with pytest.raises(SourceError) as e:
        next(iter(drv.scans()))
    assert "timeout de leitura" in str(e.value)


def test_cable_unplugged_mid_scan_is_clear_error():
    drv, fake = driver(make_device(scan_stream=make_stream(30)))
    drv.open("COM9")
    drv.start_scan()
    it = drv.scans()
    next(it)
    fake.disconnected = True
    with pytest.raises(SourceError) as e:
        for _ in it:
            pass
    assert "cabo" in str(e.value) and "desconectado" in str(e.value)
    drv.close()                                          # não levanta mesmo com a porta morta


def test_cable_unplugged_at_handshake():
    fake = FakeSerial(make_device())
    fake.disconnected = True
    drv = RplidarC1(serial_factory=lambda port, **k: fake, info_timeout=0.3)
    with pytest.raises(SourceError) as e:
        drv.open("COM9")
    assert "cabo" in str(e.value)


@pytest.mark.parametrize("exc,expect", [
    (serial.SerialException("could not open port 'COM9': PermissionError(13, 'Acesso negado.', None, 5)"), "ocupada"),
    (serial.SerialException("could not open port 'COM9': PermissionError(13, 'Access is denied.', None, 5)"), "ocupada"),
    (serial.SerialException("could not open port 'COM9': FileNotFoundError(2, 'O sistema não pode encontrar o arquivo especificado.', None, 2)"), "não encontrada"),
    (FileNotFoundError(2, "x"), "não encontrada"),
    (PermissionError(13, "x"), "ocupada"),
    (serial.SerialException("algo inesperado"), "não foi possível abrir"),
])
def test_open_errors_are_translated(exc, expect):
    def factory(port, **kw):
        raise exc
    with pytest.raises(SourceError) as e:
        RplidarC1(serial_factory=factory).open("COM9")
    assert expect in str(e.value) and "COM9" in str(e.value)
    assert "COM9" in translate_open_error("COM9", exc)


def test_protection_stop_triggers_reset_then_scans_if_recovered():
    drv, fake = driver(make_device(health_sequence=[(0, 0), (2, 0x0102), (0, 0)], scan_stream=make_stream(3)))
    drv.open("COM9")                                     # health inicial: OK
    # no start_scan, o 2º GET_HEALTH vem em Protection Stop -> RESET -> 3º vem OK
    drv.start_scan()
    assert RESET in fake.written and fake.written[-1] == SCAN
    drv.close()


def test_protection_stop_not_recovered_is_clear_error():
    drv, fake = driver(make_device(health_sequence=[(0, 0), (2, 0x0102)], scan_stream=make_stream(3)))
    drv.open("COM9")
    with pytest.raises(SourceError) as e:
        drv.start_scan()
    assert "Protection Stop" in str(e.value) and "258" in str(e.value)    # 0x0102 = 258
    assert SCAN not in fake.written


def test_warning_health_still_scans():
    drv, fake = driver(make_device(health=(1, 5), scan_stream=make_stream(3)))
    drv.open("COM9")
    assert drv.get_health().status is HealthStatus.WARNING
    drv.start_scan()
    drv.close()


def test_measurements_arriving_with_descriptor_are_not_lost():
    first = encode_measurement(0, 1000, 20, start=True) + encode_measurement(1, 1000, 20, start=False)

    def responder(req, f):
        make_device()(req, f)
        if req == SCAN:
            f.feed(first)                                # logo após o descritor, antes do 1º read

    drv, fake = driver(responder)
    drv.open("COM9")
    drv.start_scan()
    assert drv._leftover == first                        # guardado para a 1ª iteração de scans()
    drv.close()


# --- integração Sessão + API com o driver real sobre a serial falsa ----------------------------------------
def test_session_with_real_driver_exposes_info_errors_and_cable_failure():
    fake = FakeSerial(make_device(scan_stream=make_stream(200, points=500), slow=True))
    app = create_app(source_for_port=lambda port: RplidarC1(
        serial_factory=lambda p, **k: fake, info_timeout=0.5, data_timeout=0.5, first_data_timeout=1.0))
    with TestClient(app) as c:
        st = c.post("/api/connect", json={"port": "COM9"}).json()
        assert st["info"]["firmware"] == "1.03" and st["health"]["status"] == "ok" and st["errors"] == 0
        c.post("/api/scan/start")
        time.sleep(0.6)
        st = c.get("/api/status").json()
        assert st["state"] == "scanning" and st["points"] > 100
        fake.feed(b"\x00\x00\x00\x00\x00\x00\x00")       # lixo na linha: vira erro contado
        time.sleep(0.4)
        assert c.get("/api/status").json()["errors"] >= 1
        fake.disconnected = True                         # cabo puxado
        time.sleep(0.6)
        st = c.get("/api/status").json()
        assert st["state"] == "connected"                # a thread terminou sem travar
        assert "cabo" in st["error"]                     # e o erro está visível no status
        assert c.post("/api/disconnect").status_code == 200
