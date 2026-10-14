"""Testes com bytes REAIS do C1 (gerados por scripts/capture_bytes.py) + testes do próprio script.

Os testes de fixtures reais são PULADOS enquanto não houver arquivos em tests/fixtures/
(c1_*_scan.bin, c1_*_info.bin, c1_*_health.bin). Estado da etapa 6: pendente de validação com o
hardware.
"""
import importlib.util
from pathlib import Path

import pytest

from backend.sources.rplidar_protocol import (
    SEND_MULTIPLE, SEND_SINGLE, TYPE_DEVINFO, TYPE_HEALTH, TYPE_MEASUREMENT, StandardScanDecoder,
    find_descriptor, parse_device_info, parse_health,
)
from tests.fake_serial import FakeSerial, make_device, make_stream

FIXTURES = Path(__file__).parent / "fixtures"
SCANS = sorted(FIXTURES.glob("c1_*_scan.bin"))
INFOS = sorted(FIXTURES.glob("c1_*_info.bin"))
HEALTHS = sorted(FIXTURES.glob("c1_*_health.bin"))
NO_HW = "sem fixtures reais em tests/fixtures/ — pendente de validação com o hardware (rode scripts/capture_bytes.py)"


@pytest.mark.skipif(not SCANS, reason=NO_HW)
@pytest.mark.parametrize("path", SCANS, ids=lambda p: p.name)
def test_real_scan_capture(path):
    buf = bytearray(path.read_bytes())
    desc = find_descriptor(buf)
    assert desc is not None, "descritor A5 5A não encontrado na captura"
    assert (desc.length, desc.send_mode, desc.data_type) == (5, SEND_MULTIPLE, TYPE_MEASUREMENT)
    dec = StandardScanDecoder()
    scans = dec.feed(bytes(buf))
    assert len(scans) >= 5
    pts = [len(s.angle_deg) for s in scans[1:]]
    assert all(300 <= n <= 800 for n in pts), f"pontos por volta fora do esperado (~500 a 10 Hz): {pts}"
    total_packets = sum(len(s.angle_deg) for s in scans)
    assert dec.errors / max(total_packets, 1) < 0.01, "mais de 1% de erros de verificação"
    for s in scans:
        assert s.angle_deg.min() >= 0 and s.angle_deg.max() < 360


@pytest.mark.skipif(not INFOS, reason=NO_HW)
@pytest.mark.parametrize("path", INFOS, ids=lambda p: p.name)
def test_real_info_capture(path):
    buf = bytearray(path.read_bytes())
    desc = find_descriptor(buf)
    assert (desc.length, desc.send_mode, desc.data_type) == (20, SEND_SINGLE, TYPE_DEVINFO)
    info = parse_device_info(bytes(buf[:20]))
    assert info.serial and info.firmware


@pytest.mark.skipif(not HEALTHS, reason=NO_HW)
@pytest.mark.parametrize("path", HEALTHS, ids=lambda p: p.name)
def test_real_health_capture(path):
    buf = bytearray(path.read_bytes())
    desc = find_descriptor(buf)
    assert (desc.length, desc.send_mode, desc.data_type) == (3, SEND_SINGLE, TYPE_HEALTH)
    parse_health(bytes(buf[:3]))


# --- o próprio script de captura, contra a serial falsa ---------------------------------------------------
def load_script():
    path = Path(__file__).parent.parent / "scripts" / "capture_bytes.py"
    spec = importlib.util.spec_from_file_location("capture_bytes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_capture_script_writes_fixture_files_that_the_parser_accepts(tmp_path):
    mod = load_script()
    fake = FakeSerial(make_device(scan_stream=make_stream(8), slow=False))
    meta = mod.capture_session(fake, seconds=0.3, out_dir=tmp_path, port="COM9", log=lambda *_: None,
                               spinup=0.0)
    assert meta["info_ok"] and meta["health_ok"]
    assert meta["scan_sanidade"]["descritor_encontrado"] and meta["scan_sanidade"]["fracao_valida"] == 1.0
    assert fake.written[0] == b"\xA5\x25" and fake.written[-1] == b"\xA5\x25"   # STOP no início e no fim
    files = {p.name.split("_", 3)[-1]: p for p in tmp_path.iterdir()}
    assert {"info.bin", "health.bin", "scan.bin", "meta.json"} <= set(files)

    buf = bytearray(files["scan.bin"].read_bytes())            # os arquivos gerados passam nos mesmos
    assert find_descriptor(buf).data_type == TYPE_MEASUREMENT  # testes que as capturas reais usarão
    assert len(StandardScanDecoder().feed(bytes(buf))) == 7
    ibuf = bytearray(files["info.bin"].read_bytes())
    find_descriptor(ibuf)
    assert parse_device_info(bytes(ibuf)).firmware == "1.03"


def test_capture_script_sends_final_stop_even_if_device_is_silent(tmp_path):
    mod = load_script()
    fake = FakeSerial(make_device(silent=True))
    meta = mod.capture_session(fake, seconds=0.1, out_dir=tmp_path, port="COM9", log=lambda *_: None,
                               do_scan=False)
    assert not meta["info_ok"] and not meta["health_ok"]
    assert fake.written[-1] == b"\xA5\x25"
