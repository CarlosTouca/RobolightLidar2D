"""Máquina de estados e API de controle, via TestClient."""
import time

import pytest
from fastapi.testclient import TestClient

from backend.main import create_app
from backend.sources.base import SourceError
from backend.sources.simulator import Simulator


@pytest.fixture
def client():
    with TestClient(create_app(source_for_port=lambda port: Simulator(seed=0))) as c:
        yield c


def err(resp, status, code):
    assert resp.status_code == status, resp.text
    e = resp.json()["error"]
    assert e["code"] == code and e["message"]
    return e["message"]


def test_starts_disconnected_not_scanning(client):
    st = client.get("/api/status").json()
    assert st["state"] == "disconnected" and st["info"] is None and st["error"] == ""


def test_ports_always_include_simulator_first(client):
    ports = client.get("/api/ports").json()["ports"]
    assert ports[0]["id"] == "SIMULADOR" and "SIMULADOR" in ports[0]["label"]
    assert all({"id", "label"} <= set(p) for p in ports)


def test_config_defaults(client):
    cfg = client.get("/api/config").json()
    assert cfg["default_range_m"] == 6 and cfg["max_fps"] == 5 and cfg["baudrate"] == 460800


def test_start_scan_without_connecting_returns_clear_error(client):
    msg = err(client.post("/api/scan/start"), 409, "not_connected")
    assert "conecte primeiro" in msg
    err(client.post("/api/scan/stop"), 409, "not_scanning")
    err(client.post("/api/disconnect"), 409, "already_disconnected")
    assert client.get("/api/status").json()["state"] == "disconnected"   # nada mudou


def test_full_state_machine(client):
    r = client.post("/api/connect", json={"port": "SIMULADOR"})
    assert r.status_code == 200
    st = r.json()
    assert st["state"] == "connected" and st["port"] == "SIMULADOR"
    assert st["info"] == {"model": "SIMULADOR", "firmware": "1.0", "hardware": "0",
                          "serial": "SIM-0001"}
    assert st["health"] == {"status": "ok", "error_code": 0}

    err(client.post("/api/connect", json={"port": "SIMULADOR"}), 409, "already_connected")
    err(client.post("/api/scan/stop"), 409, "not_scanning")

    assert client.post("/api/scan/start").json()["state"] == "scanning"
    err(client.post("/api/scan/start"), 409, "already_scanning")
    time.sleep(0.4)
    assert client.get("/api/status").json()["points"] > 100

    st = client.post("/api/scan/stop").json()
    assert st["state"] == "connected" and st["points"] == 0
    assert client.post("/api/scan/start").json()["state"] == "scanning"   # reinicia

    st = client.post("/api/disconnect").json()                             # com varredura ativa
    assert st["state"] == "disconnected" and st["info"] is None and st["port"] == ""
    err(client.post("/api/disconnect"), 409, "already_disconnected")

    assert client.post("/api/connect", json={"port": "SIMULADOR"}).status_code == 200  # reconecta


def test_connect_failure_is_502_with_message_and_stays_disconnected(client):
    msg = err(client.post("/api/connect", json={"port": "COM99"}), 502, "source_error")
    assert "COM99" in msg
    assert client.get("/api/status").json()["state"] == "disconnected"


def test_connect_requires_port_field(client):
    assert client.post("/api/connect", json={}).status_code == 422


def test_source_error_during_scan_is_exposed_in_status():
    class Breaks(Simulator):
        def scans(self):
            raise SourceError("timeout de leitura da serial")
            yield

    with TestClient(create_app(source_for_port=lambda port: Breaks(seed=0))) as c:
        c.post("/api/connect", json={"port": "SIMULADOR"})
        c.post("/api/scan/start")
        time.sleep(0.3)
        st = c.get("/api/status").json()
        assert st["state"] == "connected"
        assert "timeout de leitura da serial" in st["error"]


def test_default_factory_routes_com_ports_to_real_driver_without_touching_hardware(monkeypatch):
    """A fábrica padrão manda COMx ao driver do C1. A abertura da serial é substituída por uma
    falha simulada: o teste nunca abre uma porta real (as COM do PC podem ser Bluetooth)."""
    import serial
    opened = []

    def fake_open(port, **kw):
        opened.append((port, kw["baudrate"]))
        raise serial.SerialException(f"could not open port {port!r}: PermissionError(13, 'Acesso negado.')")

    monkeypatch.setattr("backend.sources.rplidar_c1._default_factory", fake_open)
    with TestClient(create_app()) as c:
        msg = err(c.post("/api/connect", json={"port": "COM7"}), 502, "source_error")
        assert "COM7" in msg and "ocupada" in msg
        assert opened == [("COM7", 460800)]               # 460800 baud, conforme o datasheet
        assert c.get("/api/status").json()["state"] == "disconnected"


def test_format_port_shows_description_next_to_name():
    from backend.sources.factory import format_port
    p = format_port("COM7", "Serial Padrão por link Bluetooth (COM7)")
    assert p.label == "COM7 — Serial Padrão por link Bluetooth" and not p.usb
    p = format_port("COM9", "Silicon Labs CP210x USB to UART Bridge (COM9)", 0x10C4, 0xEA60)
    assert p.label == "COM9 — Silicon Labs CP210x USB to UART Bridge [USB 10C4:EA60]" and p.usb
    assert format_port("COM1", "").label == "COM1 — sem descrição"
    assert format_port("COM2", "n/a").label == "COM2 — sem descrição"


def test_usb_ports_listed_before_bluetooth_ones(monkeypatch):
    import serial.tools.list_ports as lp
    from types import SimpleNamespace as NS
    fake = [NS(device="COM3", description="Serial Padrão por link Bluetooth (COM3)", vid=None, pid=None),
            NS(device="COM12", description="Silicon Labs CP210x USB to UART Bridge (COM12)",
               vid=0x10C4, pid=0xEA60),
            NS(device="COM4", description="Serial Padrão por link Bluetooth (COM4)", vid=None, pid=None)]
    monkeypatch.setattr(lp, "comports", lambda: fake)
    from backend.sources.factory import list_ports
    ids = [p.id for p in list_ports()]
    assert ids == ["SIMULADOR", "COM12", "COM3", "COM4"]


def test_drive_needs_connection_and_validates_body(client):
    assert client.post("/api/drive", json={"cmd": "F", "speed": 150}).status_code == 409
    client.post("/api/connect", json={"port": "SIMULADOR"})
    assert client.post("/api/drive", json={"cmd": "G"}).json() == {"ok": True}   # speed é opcional
    assert client.post("/api/drive", json={"cmd": "X", "speed": 150}).status_code == 422
    assert client.post("/api/drive", json={"cmd": "F", "speed": 300}).status_code == 422
