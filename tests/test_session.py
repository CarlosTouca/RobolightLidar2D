import time

import pytest

from backend.hub import Hub
from backend.session import Session, SessionError
from backend.sources.base import SourceError
from backend.sources.simulator import Simulator


def make_session(**kw):
    hub = Hub()
    return hub, Session(hub, source_for_port=lambda port: Simulator(seed=0, **kw))


def code_of(excinfo) -> str:
    return excinfo.value.code


def test_invalid_transitions_have_codes_and_clear_messages():
    _, s = make_session()
    with pytest.raises(SessionError) as e:
        s.start_scan()
    assert code_of(e) == "not_connected" and "conecte primeiro" in e.value.message
    with pytest.raises(SessionError) as e:
        s.stop_scan()
    assert code_of(e) == "not_scanning"
    with pytest.raises(SessionError) as e:
        s.disconnect()
    assert code_of(e) == "already_disconnected"

    s.connect("SIMULADOR")
    with pytest.raises(SessionError) as e:
        s.connect("SIMULADOR")
    assert code_of(e) == "already_connected"
    with pytest.raises(SessionError) as e:
        s.stop_scan()                          # conectado, mas sem varrer
    assert code_of(e) == "not_scanning"

    s.start_scan()
    with pytest.raises(SessionError) as e:
        s.start_scan()
    assert code_of(e) == "already_scanning"
    with pytest.raises(SessionError) as e:
        s.connect("SIMULADOR")
    assert code_of(e) == "already_connected"
    s.shutdown()
    s.shutdown()                               # shutdown é idempotente


def test_bad_port_leaves_session_disconnected():
    _, s = make_session()
    with pytest.raises(SourceError):
        s.connect("COM99")
    assert s.state == "disconnected"


def test_scan_cycle_publishes_filtered_frames():
    hub, s = make_session(dropout=0.1)
    s.connect("SIMULADOR")
    st = s.status()
    assert st.state == "connected" and st.info.model == "SIMULADOR" and st.health.status.value == "ok"

    s.start_scan()
    assert s.state == "scanning"
    time.sleep(0.55)
    assert hub.seq >= 4
    assert 0 < s.last_points < 500           # perdas (d=0) foram descartadas no backend

    s.stop_scan()
    assert s.state == "connected" and s.status().points == 0
    seq = hub.seq
    time.sleep(0.3)
    assert hub.seq == seq                    # nada mais é publicado após parar

    s.start_scan()                           # pode reiniciar
    time.sleep(0.25)
    assert hub.seq > seq
    s.disconnect()
    assert s.state == "disconnected" and s.status().info is None


def test_disconnect_while_scanning_stops_thread_and_clears_hub():
    hub, s = make_session()
    s.connect("SIMULADOR")
    s.start_scan()
    time.sleep(0.3)
    thread = s._thread
    assert thread.is_alive() and hub._latest is not None
    s.disconnect()
    assert not thread.is_alive()
    assert s.state == "disconnected"
    assert hub._latest is None               # sem quadro "congelado" para clientes novos
    seq = hub.seq
    time.sleep(0.3)
    assert hub.seq == seq


def test_source_failure_returns_to_connected_with_visible_error():
    class Broken(Simulator):
        def scans(self):
            raise SourceError("cabo desconectado")
            yield

    hub = Hub()
    s = Session(hub, source_for_port=lambda port: Broken(seed=0))
    s.connect("SIMULADOR")
    s.start_scan()
    time.sleep(0.2)
    assert s.state == "connected"
    assert "cabo desconectado" in s.status().error
    s.start_scan()                           # novo início limpa o erro anterior
    assert s.status().error == ""
    time.sleep(0.2)
    s.disconnect()


def test_unexpected_exception_is_reported_not_swallowed():
    class Buggy(Simulator):
        def scans(self):
            raise ValueError("bug")
            yield

    s = Session(Hub(), source_for_port=lambda port: Buggy(seed=0))
    s.connect("SIMULADOR")
    s.start_scan()
    time.sleep(0.2)
    assert s.state == "connected" and "ValueError" in s.status().error
    s.shutdown()
