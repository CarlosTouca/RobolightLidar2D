import json
import socket
import threading
import time

import pytest
import uvicorn
from websockets.sync.client import connect as _connect


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server():
    """Fábrica de servidores uvicorn reais (thread própria) para testes ponta a ponta."""
    started = []

    def start(app):
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "servidor de teste não subiu"
        started.append((server, thread))
        return port, server

    yield start
    for server, thread in started:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "o servidor de teste não encerrou em 10 s"


class _ScanOnly:
    """Cliente WebSocket de teste: recv() só devolve varreduras. A pose do rover vai a ~60/s por outro canal
    (`{"type": "pose"}`, sem ack) e é ignorada aqui, sem gastar o prazo do recv."""

    def __init__(self, ws):
        self._ws = ws

    def recv(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            raw = self._ws.recv(timeout=remaining)
            if json.loads(raw).get("type") != "pose":
                return raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._ws.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._ws, name)


def connect(url):
    return _ScanOnly(_connect(url))
