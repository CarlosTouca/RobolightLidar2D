"""Teste ponta a ponta: servidor uvicorn real + cliente WebSocket real."""
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from tests.conftest import connect  # noqa: E402
from backend.config import MAX_FPS
from backend.main import create_app


@pytest.fixture
def server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    base = f"http://127.0.0.1:{port}"
    httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}).raise_for_status()
    httpx.post(f"{base}/api/scan/start").raise_for_status()      # o painel faz isto na UI
    yield port, server
    server.should_exit = True
    thread.join(timeout=5)


def test_websocket_stream_rate_and_format(server):
    server_port = server[0]
    times, msgs = [], []
    with connect(f"ws://127.0.0.1:{server_port}/ws") as ws:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.2:
            raw = ws.recv(timeout=1.0)
            times.append(time.monotonic())
            msgs.append(json.loads(raw))
            ws.send(str(msgs[-1]["seq"]))               # ack
        elapsed = time.monotonic() - t0

    assert MAX_FPS * elapsed * 0.6 <= len(msgs) <= MAX_FPS * elapsed + 1      # ~MAX_FPS/s
    seqs = [m["seq"] for m in msgs]
    assert all(b > a for a, b in zip(seqs, seqs[1:]))
    m = msgs[0]
    assert set(m) == {"type", "seq", "t", "x", "y", "q", "pose"}   # pose: só o simulador envia
    assert len(m["pose"]) == 3
    assert len(m["x"]) == len(m["y"]) == len(m["q"]) > 100
    assert all(type(v) is int for v in m["x"] + m["y"] + m["q"])  # inteiros, sem objetos por ponto
    assert min(m["q"]) > 0                                          # q=0 descartado no backend


def test_client_disconnect_is_cleaned_up(server):
    server_port = server[0]
    for _ in range(3):
        with connect(f"ws://127.0.0.1:{server_port}/ws") as ws:
            ws.recv(timeout=1.0)
    with connect(f"ws://127.0.0.1:{server_port}/ws") as ws:         # servidor segue funcionando
        assert json.loads(ws.recv(timeout=1.0))["type"] == "scan"


def test_slow_websocket_client_lag_stays_bounded(server):
    """Leitor lento (0,8 s por mensagem): sem controle de fluxo os buffers TCP guardariam
    dezenas de varreduras velhas e o atraso cresceria sem parar (~25+ varreduras em 4 s)."""
    port, srv = server
    hub = srv.config.app.state.hub
    lags = []
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4.0:
            msg = json.loads(ws.recv(timeout=3.0))
            lags.append(hub.seq - msg["seq"])         # varreduras produzidas desde esta
            time.sleep(0.8)                           # "renderização" lenta
            ws.send(str(msg["seq"]))
    assert len(lags) >= 4
    assert max(lags) <= 5                             # <= ~0,5 s de atraso, não cresce


def test_reconnect_after_unacked_message_resumes_immediately(server):
    port = server[0]
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:
        ws.recv(timeout=1.0)                          # recebe e NÃO dá ack: fica em trânsito
        time.sleep(0.5)                               # o servidor segura as próximas
    t0 = time.monotonic()
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:  # reconexão: estado em trânsito zerado
        msg = json.loads(ws.recv(timeout=1.0))
    assert msg["type"] == "scan"
    assert time.monotonic() - t0 < 1.0                # bem menos que o timeout de ack (2 s)


def test_disconnect_with_active_scan_silences_stream_and_new_clients_get_nothing(server):
    port = server[0]
    base = f"http://127.0.0.1:{port}"
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:
        msg = json.loads(ws.recv(timeout=1.0))
        ws.send(str(msg["seq"]))
        assert httpx.post(f"{base}/api/disconnect").json()["state"] == "disconnected"
        # a conexão WebSocket continua aberta, mas não chegam mais varreduras
        deadline = time.monotonic() + 1.0
        late = []
        while time.monotonic() < deadline:
            try:
                late.append(ws.recv(timeout=0.3))
            except TimeoutError:
                pass
        assert len(late) <= 1                       # no máximo uma já em trânsito
    with connect(f"ws://127.0.0.1:{port}/ws") as ws2:   # cliente novo: sem quadro congelado
        with pytest.raises(TimeoutError):
            ws2.recv(timeout=0.8)
    httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}).raise_for_status()
    httpx.post(f"{base}/api/scan/start").raise_for_status()
    with connect(f"ws://127.0.0.1:{port}/ws") as ws3:    # e o fluxo volta ao reconectar/varrer
        assert json.loads(ws3.recv(timeout=1.5))["type"] == "scan"
