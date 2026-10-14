import socket
import threading
import time

import pytest
import uvicorn


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
