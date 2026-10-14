"""Etapa 7 — robustez, SÓ com o simulador e a serial falsa (nenhum teste depende do aparelho).

Falhas físicas aqui são SIMULADAS (Simulator.inject_fault, FakeSerial): provam que o software
reage bem, não que o C1 real se comporta assim.
"""
import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import serial
from fastapi.testclient import TestClient
from websockets.sync.client import connect

from backend.hub import Hub
from backend.main import create_app
from backend.models import Health, HealthStatus
from backend.session import Session, SessionError
from backend.sources.base import SourceError
from backend.sources.rplidar_c1 import RplidarC1
from backend.sources.simulator import Simulator, simulate_scan
from tests.conftest import free_port
from tests.fake_serial import FakeSerial, make_device, make_stream

ROOT = Path(__file__).resolve().parent.parent


def readers_alive() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "lidar-reader" and t.is_alive()]


def wait_until(cond, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def sim_app(**kw):
    sims = []

    def factory(port):
        sims.append(Simulator(seed=0, **kw))
        return sims[-1]

    return create_app(source_for_port=factory), sims


def err(resp, status, code):
    assert resp.status_code == status, resp.text
    e = resp.json()["error"]
    assert e["code"] == code and e["message"]
    return e["message"]


# --- desconexão física simulada ---------------------------------------------------------------------------
def test_simulated_cable_pull_during_scan_is_reported_and_recoverable():
    app, sims = sim_app()
    with TestClient(app) as c:
        c.post("/api/connect", json={"port": "SIMULADOR"})
        c.post("/api/scan/start")
        hub = app.state.hub
        assert wait_until(lambda: hub.seq >= 3)

        sims[-1].inject_fault("disconnect")               # cabo puxado
        assert wait_until(lambda: c.get("/api/status").json()["state"] == "connected")
        st = c.get("/api/status").json()
        assert "desconectado" in st["error"] and st["points"] == 0     # erro visível, tela sem pontos
        assert hub._latest is None                                     # nenhum quadro "congelado"
        seq = hub.seq
        time.sleep(0.4)
        assert hub.seq == seq                                          # nada mais é publicado
        assert wait_until(lambda: not readers_alive())                 # a thread terminou

        msg = err(c.post("/api/scan/start"), 502, "source_error")      # reiniciar com cabo solto
        assert "desconectado" in msg
        assert c.post("/api/disconnect").status_code == 200            # soltar a sessão quebrada
        assert c.post("/api/connect", json={"port": "SIMULADOR"}).status_code == 200   # "recolocou o cabo"
        assert c.post("/api/scan/start").json()["state"] == "scanning"
        assert wait_until(lambda: hub.seq > seq)                       # fluxo volta
        assert c.get("/api/status").json()["error"] == ""


def test_fault_before_connect_attempt_is_a_clear_error():
    app, sims = sim_app()
    with TestClient(app) as c:
        c.post("/api/connect", json={"port": "SIMULADOR"})
        sims[-1].inject_fault("disconnect")
        err(c.post("/api/scan/start"), 502, "source_error")
        assert c.get("/api/status").json()["state"] == "connected"     # estado coerente, sem thread


# --- health em erro (Protection Stop) ----------------------------------------------------------------------------
def test_health_error_is_visible_and_blocks_scan_until_recovered():
    app, sims = sim_app(health=Health(HealthStatus.ERROR, 258))
    with TestClient(app) as c:
        st = c.post("/api/connect", json={"port": "SIMULADOR"}).json()
        assert st["health"] == {"status": "error", "error_code": 258}   # aparece no painel
        msg = err(c.post("/api/scan/start"), 502, "source_error")
        assert "Protection Stop" in msg and "258" in msg
        assert c.get("/api/status").json()["state"] == "connected" and not readers_alive()

        sims[-1].set_health(Health(HealthStatus.OK))                    # "RESET resolveu"
        assert c.post("/api/scan/start").json()["state"] == "scanning"


def test_health_warning_still_scans():
    app, _ = sim_app(health=Health(HealthStatus.WARNING, 5))
    with TestClient(app) as c:
        assert c.post("/api/connect", json={"port": "SIMULADOR"}).json()["health"]["status"] == "warning"
        assert c.post("/api/scan/start").json()["state"] == "scanning"


# --- porta ocupada ---------------------------------------------------------------------------------------------------
def test_busy_port_then_retry_succeeds():
    attempts = []

    def serial_factory(port, **kw):
        attempts.append(port)
        if len(attempts) == 1:
            raise serial.SerialException(
                f"could not open port {port!r}: PermissionError(13, 'Acesso negado.', None, 5)")
        return FakeSerial(make_device())

    app = create_app(source_for_port=lambda port: RplidarC1(serial_factory=serial_factory, info_timeout=0.5))
    with TestClient(app) as c:
        msg = err(c.post("/api/connect", json={"port": "COM9"}), 502, "source_error")
        assert "ocupada" in msg and "COM9" in msg
        st = c.get("/api/status").json()
        assert st["state"] == "disconnected" and st["info"] is None
        st = c.post("/api/connect", json={"port": "COM9"}).json()      # o outro programa soltou a porta
        assert st["state"] == "connected" and st["info"]["firmware"] == "1.03"


def test_device_that_never_answers_times_out_and_session_stays_usable():
    app = create_app(source_for_port=lambda port: RplidarC1(
        serial_factory=lambda p, **k: FakeSerial(make_device(silent=True)), info_timeout=0.2))
    with TestClient(app) as c:
        t0 = time.monotonic()
        msg = err(c.post("/api/connect", json={"port": "COM3"}), 502, "source_error")
        assert "sem resposta" in msg and time.monotonic() - t0 < 2.0
        assert c.get("/api/status").json()["state"] == "disconnected"


# --- thread de leitura travada / erro inesperado --------------------------------------------------------------
class Stubborn(Simulator):
    """scans() ignora stop_scan até `release` (fonte travada em I/O)."""

    def __init__(self):
        super().__init__(seed=0)
        self.release = threading.Event()

    def scans(self):
        rng = np.random.default_rng(0)
        yield simulate_scan(self._scene, rng)
        self.release.wait(5)
        yield simulate_scan(self._scene, rng)    # tentaria publicar DEPOIS do disconnect


def test_stuck_reader_is_reported_blocks_restart_and_never_publishes_after_disconnect():
    hub = Hub()
    src = Stubborn()
    s = Session(hub, source_for_port=lambda p: src, join_timeout=0.2)
    s.connect("SIMULADOR")
    s.start_scan()
    assert wait_until(lambda: hub.seq == 1)
    s.stop_scan()                                         # a thread não obedece
    st = s.status()
    assert st.state == "connected" and "não encerrou" in st.error
    with pytest.raises(SessionError) as e:
        s.start_scan()
    assert e.value.code == "reader_busy"                  # nada de dois leitores ao mesmo tempo
    s.disconnect()                                        # descarta a sessão
    assert s.state == "disconnected"
    src.release.set()                                     # a thread antiga acorda e tenta publicar
    assert wait_until(lambda: not readers_alive())
    assert hub.seq == 1 and hub._latest is None           # ... mas nada vaza para os clientes


def test_unexpected_exception_clears_source_scanning_flag_and_allows_restart():
    class Buggy(Simulator):
        calls = 0

        def scans(self):
            Buggy.calls += 1
            if Buggy.calls == 1:
                raise ValueError("bug")
            yield from super().scans()

    src = Buggy(seed=0)
    s = Session(Hub(), source_for_port=lambda p: src)
    s.connect("SIMULADOR")
    s.start_scan()
    assert wait_until(lambda: s.state == "connected")
    assert "ValueError" in s.status().error
    assert not src._scanning.is_set()                     # a fonte foi parada, não ficou "varrendo"
    s.start_scan()                                        # e dá para tentar de novo
    assert wait_until(lambda: s.last_points > 0)
    s.shutdown()


def test_status_is_not_blocked_by_slow_connect_and_concurrent_connect_is_rejected(live_server):
    class SlowOpen(Simulator):
        def open(self, port):
            time.sleep(1.0)                               # p.ex. timeout da serial
            super().open(port)

    port, _ = live_server(create_app(source_for_port=lambda p: SlowOpen(seed=0)))
    base = f"http://127.0.0.1:{port}"
    results = {}
    t1 = threading.Thread(target=lambda: results.update(first=httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}, timeout=10)))
    t1.start()
    time.sleep(0.2)
    t0 = time.monotonic()
    st = httpx.get(f"{base}/api/status", timeout=5).json()
    assert time.monotonic() - t0 < 0.8 and st["state"] == "disconnected"    # painel não congela
    second = httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}, timeout=10)
    t1.join()
    assert results["first"].status_code == 200
    assert second.status_code == 409 and second.json()["error"]["code"] == "already_connected"


# --- estresse de concorrência ----------------------------------------------------------------------------------------
def test_concurrent_command_storm_keeps_invariants_and_leaves_no_thread():
    hub = Hub()
    s = Session(hub, source_for_port=lambda p: Simulator(seed=1), join_timeout=2.0)
    unexpected = []
    stop = time.monotonic() + 3.0

    def worker(seed):
        rng = random.Random(seed)
        ops = [lambda: s.connect("SIMULADOR"), s.start_scan, s.stop_scan, s.disconnect, s.status]
        while time.monotonic() < stop:
            try:
                rng.choice(ops)()
            except (SessionError, SourceError):
                pass
            except Exception as e:                        # qualquer outra coisa é bug
                unexpected.append(repr(e))
            time.sleep(rng.random() * 0.01)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert unexpected == []
    s.shutdown()
    assert s.state == "disconnected"
    assert wait_until(lambda: not readers_alive())
    assert hub._latest is None


# --- WebSocket: clientes ruins, quedas bruscas, muitos clientes -------------------------------------------------
def started_app(live_server):
    app = create_app()
    port, server = live_server(app)
    base = f"http://127.0.0.1:{port}"
    httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}).raise_for_status()
    httpx.post(f"{base}/api/scan/start").raise_for_status()
    return port, app.state.hub, base


def test_garbage_from_client_is_ignored_and_stream_keeps_working(live_server):
    port, hub, _ = started_app(live_server)
    junk = [b"\x00\x01\x02", "x" * 5000, "9" * 5000, "²", "-5", "1.5", "", "   ", "99999999999999",
            "٣", "seq", json.dumps({"ack": 1})]            # inclui dígitos não-ASCII e número de 5000 dígitos
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:
        got = 0
        for j in junk:
            ws.send(j)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.8:
            msg = json.loads(ws.recv(timeout=1.5))
            got += 1
            ws.send(str(msg["seq"]))                      # ack válido continua funcionando
        assert got >= 6                                   # ~5 msg/s: fluxo normal apesar do lixo
    assert wait_until(lambda: hub.client_count == 0)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_abrupt_disconnects_do_not_leak_channels(live_server):
    port, hub, _ = started_app(live_server)
    for _ in range(30):
        ws = connect(f"ws://127.0.0.1:{port}/ws")
        ws.recv(timeout=1.5)
        ws.socket.shutdown(socket.SHUT_RDWR)              # sem handshake de fechamento: rede caiu
        ws.socket.close()
    assert wait_until(lambda: hub.client_count == 0, timeout=5)
    with connect(f"ws://127.0.0.1:{port}/ws") as ws:      # servidor segue saudável
        assert json.loads(ws.recv(timeout=1.5))["type"] == "scan"


def test_many_clients_one_never_acks_does_not_affect_the_others(live_server):
    port, hub, _ = started_app(live_server)
    counts, lock = {}, threading.Lock()

    def client(i, acks):
        n = 0
        with connect(f"ws://127.0.0.1:{port}/ws") as ws:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.6:
                try:
                    msg = json.loads(ws.recv(timeout=0.5))
                except TimeoutError:
                    continue
                n += 1
                if acks:
                    ws.send(str(msg["seq"]))
        with lock:
            counts[i] = n

    threads = [threading.Thread(target=client, args=(i, i != 0)) for i in range(25)]   # cliente 0 nunca dá ack
    for t in threads:
        t.start()
    assert wait_until(lambda: hub.client_count == 25, timeout=3)
    for t in threads:
        t.join()
    fast = [counts[i] for i in range(1, 25)]
    assert min(fast) >= 8 and max(fast) <= 5 * 2.6 + 2     # ~5/s cada, nenhum passa do limite
    assert counts[0] <= 3                                   # sem ack: só o gotejamento do timeout (2 s)
    assert wait_until(lambda: hub.client_count == 0)


def test_hub_stop_cancels_every_client_task():
    async def main():
        hub = Hub(fps=20)
        await hub.start()

        async def send(msg):
            await asyncio.sleep(10)                        # clientes travados em envio

        chans = [hub.add_client(send, flow_control=True) for _ in range(5)]
        hub.publish(__import__("backend.models", fromlist=["Frame"]).Frame([1], [2], [3]))
        await asyncio.sleep(0.15)
        await hub.stop()
        await asyncio.sleep(0.05)
        return [c._task.done() for c in chans], hub.client_count, hub._task

    done, count, task = asyncio.run(main())
    assert all(done) and count == 0 and task is None


# --- encerramento limpo do servidor (processo real) ------------------------------------------------------------------
def test_server_process_shuts_down_cleanly_with_client_connected_and_scan_active():
    port = free_port()
    env = {**os.environ, "LIDAR_PORT": str(port), "PYTHONUNBUFFERED": "1"}
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
    proc = subprocess.Popen([sys.executable, "-m", "backend.main"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{base}/api/status", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            pytest.fail("servidor não subiu")
        httpx.post(f"{base}/api/connect", json={"port": "SIMULADOR"}).raise_for_status()
        httpx.post(f"{base}/api/scan/start").raise_for_status()
        with connect(f"ws://127.0.0.1:{port}/ws") as ws:
            assert json.loads(ws.recv(timeout=2))["type"] == "scan"
            # Ctrl+C equivalente: CTRL_BREAK no Windows (grupo de processos próprio), SIGINT no resto
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("o servidor não encerrou em 10 s com cliente conectado e varredura ativa")
        output = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "Application shutdown complete" in output, output
    assert "Traceback" not in output, output
    with socket.socket() as s:                              # a porta foi liberada
        s.bind(("127.0.0.1", port))
    # O uvicorn termina o desligamento gracioso e então re-emite o sinal capturado, por isso o
    # código de saída não é 0: no Windows (CTRL_BREAK) sai 3; em POSIX (SIGINT) sai 130 ou -2.
    # O que prova o encerramento limpo são as linhas de log e a ausência de Traceback acima.
    assert code in ((3,) if os.name == "nt" else (0, 130, -signal.SIGINT)), code
