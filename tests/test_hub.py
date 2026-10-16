import asyncio
import json
import threading
import time
import tracemalloc

from backend.hub import Hub
from backend.models import Frame


def make_frame(n: int = 500, base: int = 1000) -> Frame:
    return Frame(x=[base + i for i in range(n)], y=[-base - i for i in range(n)], q=[30] * n)


class Producer:
    """Thread que publica quadros continuamente (como a thread de leitura da sessão)."""

    def __init__(self, hub: Hub, frames: list[Frame], period: float = 0.005):
        self._hub, self._frames, self._period = hub, frames, period
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        i = 0
        while not self._stop.is_set():
            self._hub.publish(self._frames[i % len(self._frames)])
            i += 1
            time.sleep(self._period)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        self._stop.set()
        self._thread.join()


def test_fast_client_rate_limited_to_5_per_second_with_increasing_seq():
    async def main():
        hub = Hub(fps=5)
        await hub.start()
        times, seqs = [], []

        async def send(msg):
            times.append(time.monotonic())
            seqs.append(json.loads(msg)["seq"])

        hub.add_client(send)
        t0 = time.monotonic()
        with Producer(hub, [make_frame()], period=0.005):      # ~200 quadros/s publicados
            await asyncio.sleep(2.2)
        elapsed = time.monotonic() - t0
        published = hub.seq
        await hub.stop()
        return times, seqs, elapsed, published

    times, seqs, elapsed, published = asyncio.run(main())
    assert published > 100                                  # a fonte é bem mais rápida que 5 Hz
    assert len(times) <= 5 * elapsed + 1                    # no máximo 5 msg/s
    assert len(times) >= 5 * elapsed * 0.7                  # mas realmente entrega ~5/s
    assert all(b > a for a, b in zip(seqs, seqs[1:]))       # seq estritamente crescente
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps) >= 0.17                                # sem rajadas


def test_slow_client_gets_newest_frame_and_nothing_accumulates():
    async def main():
        hub = Hub(fps=5)
        await hub.start()
        gate = asyncio.Event()
        slow_seqs, fast_seqs = [], []

        async def slow_send(msg):
            slow_seqs.append(json.loads(msg)["seq"])
            await gate.wait()                               # cliente travado

        async def fast_send(msg):
            fast_seqs.append(json.loads(msg)["seq"])

        slow = hub.add_client(slow_send)
        hub.add_client(fast_send)

        # Quadros grandes (~230 KB de JSON cada): uma fila acumulada seria visível na memória.
        # (tracemalloc deixa a codificação lenta, por isso não passam de 15 mil pontos.)
        frames = [make_frame(15_000, base=b) for b in (1000, 2000, 3000)]
        max_pending = 0
        tracemalloc.start()
        with Producer(hub, frames, period=0.02):
            await asyncio.sleep(0.9)                        # estado estacionário
            before = tracemalloc.get_traced_memory()[0]
            end = time.monotonic() + 1.6
            while time.monotonic() < end:
                max_pending = max(max_pending, slow.pending)
                await asyncio.sleep(0.01)
            growth = tracemalloc.get_traced_memory()[0] - before
            published_while_blocked = hub.seq
            sent_while_blocked = len(slow_seqs)
            dropped = slow.dropped
            gate.set()                                      # libera o cliente lento
            await asyncio.sleep(0.05)
        tracemalloc.stop()
        final_seq = hub.seq                                 # produtor parado
        await asyncio.sleep(0.6)                            # >= 1 tick
        await hub.stop()
        return (slow_seqs, fast_seqs, max_pending, growth, published_while_blocked,
                sent_while_blocked, dropped, final_seq)

    (slow_seqs, fast_seqs, max_pending, growth, published, sent_blocked,
     dropped, final_seq) = asyncio.run(main())

    assert published > 10                                   # produzidos muito mais do que enviados
    assert sent_blocked == 1                                # só 1 envio em curso, travado
    assert max_pending <= 1                                 # fila de tamanho 1, sempre
    assert dropped >= 4                                     # mensagens antigas descartadas
    assert growth < 400_000                                 # memória não cresce (fila ilimitada: ~1,8 MB)
    assert slow_seqs[-1] == final_seq                       # ao destravar, recebe o mais recente
    assert len(slow_seqs) <= 4                              # e não o backlog de dezenas de quadros
    assert all(b > a for a, b in zip(slow_seqs, slow_seqs[1:]))
    assert len(fast_seqs) >= 7                              # cliente rápido não é afetado pelo lento


def test_publish_without_clients_then_late_client_gets_latest():
    async def main():
        hub = Hub(fps=5)
        await hub.start()
        for i in range(50):
            hub.publish(make_frame(10, base=i))
        await asyncio.sleep(0.45)                           # ticks sem clientes
        got = []

        async def send(msg):
            got.append(json.loads(msg))

        hub.add_client(send)
        await asyncio.sleep(0.45)
        await hub.stop()
        return got

    got = asyncio.run(main())
    assert len(got) == 1 and got[0]["seq"] == 50


def test_message_is_compact_arrays_only():
    async def main():
        hub = Hub(fps=20)
        await hub.start()
        got = []

        async def send(msg):
            got.append(msg)

        hub.add_client(send)
        hub.publish(Frame(x=[1, 2, 3], y=[4, 5, 6], q=[7, 8, 9]))
        await asyncio.sleep(0.2)
        await hub.stop()
        return got[0]

    raw = asyncio.run(main())
    msg = json.loads(raw)
    assert set(msg) == {"type", "seq", "t", "x", "y", "q"}
    assert msg["type"] == "scan" and msg["x"] == [1, 2, 3] and msg["y"] == [4, 5, 6]
    assert ", " not in raw and ": " not in raw              # separadores compactos


def test_failed_client_is_removed_and_others_continue():
    async def main():
        hub = Hub(fps=20)
        await hub.start()
        good = []

        async def bad(msg):
            raise ConnectionError("caiu")

        async def ok(msg):
            good.append(msg)

        hub.add_client(bad)
        hub.add_client(ok)
        for _ in range(5):
            hub.publish(make_frame(10))
            await asyncio.sleep(0.1)
        count = hub.client_count
        await hub.stop()
        return count, len(good)

    count, n_good = asyncio.run(main())
    assert count == 1 and n_good >= 3


def test_stop_closes_clients():
    async def main():
        hub = Hub(fps=20)
        await hub.start()

        async def send(msg):
            pass

        hub.add_client(send)
        await hub.stop()
        return hub.client_count

    assert asyncio.run(main()) == 0


def test_flow_control_limits_inflight_and_delivers_latest_on_ack():
    async def main():
        hub = Hub(fps=20)
        await hub.start()
        got = []

        async def send(msg):                    # envio instantâneo, mas o cliente não dá ack
            got.append(json.loads(msg)["seq"])

        ch = hub.add_client(send, flow_control=True)
        with Producer(hub, [make_frame(50)], period=0.01):
            await asyncio.sleep(1.0)
            assert len(got) == 1 and ch.inflight == 1       # janela de 1, mesmo com ticks livres
            assert ch.pending <= 1
        final = hub.seq
        await asyncio.sleep(0.15)               # um tick depois do produtor parar
        dropped = ch.dropped
        ch.ack(got[-1])                         # cliente processou tudo o que recebeu
        await asyncio.sleep(0.15)
        out = list(got)
        await hub.stop()
        return out, final, dropped

    got, final, dropped = asyncio.run(main())
    assert dropped > 5                          # dezenas de ticks descartados na janela cheia
    assert len(got) == 2 and got[-1] == final   # após o ack, recebe o MAIS RECENTE, não o backlog


def test_ack_timeout_releases_flow_when_client_never_acks():
    async def main():
        hub = Hub(fps=20, ack_timeout=0.3)
        await hub.start()
        got = []

        async def send(msg):
            got.append(json.loads(msg)["seq"])

        ch = hub.add_client(send, flow_control=True)    # nunca dá ack
        with Producer(hub, [make_frame(50)], period=0.01):
            await asyncio.sleep(1.2)
        out, timeouts = list(got), ch.ack_timeouts
        await hub.stop()
        return out, timeouts

    got, timeouts = asyncio.run(main())
    assert 3 <= len(got) <= 5                           # ~1 envio a cada 0,3 s (+ tick), não travou
    assert timeouts >= 2
    assert all(b > a for a, b in zip(got, got[1:]))


def test_ack_before_timeout_does_not_count_as_timeout():
    async def main():
        hub = Hub(fps=20, ack_timeout=0.3)
        await hub.start()
        ch = None
        got = []

        async def send(msg):
            seq = json.loads(msg)["seq"]
            got.append(seq)
            asyncio.get_running_loop().call_later(0.02, ch.ack, seq)    # cliente rápido

        ch = hub.add_client(send, flow_control=True)
        with Producer(hub, [make_frame(50)], period=0.01):
            await asyncio.sleep(1.0)
        out, timeouts = list(got), ch.ack_timeouts
        await hub.stop()
        return out, timeouts

    got, timeouts = asyncio.run(main())
    assert timeouts == 0 and len(got) >= 15             # fluxo normal a ~20 msg/s (fps=20)


def test_new_connection_starts_with_clean_inflight_state():
    async def main():
        hub = Hub(fps=20, ack_timeout=30.0)             # timeout longo: só a reconexão pode liberar
        await hub.start()
        got_a, got_b = [], []

        async def send_a(msg):
            got_a.append(json.loads(msg)["seq"])

        async def send_b(msg):
            got_b.append(json.loads(msg)["seq"])

        a = hub.add_client(send_a, flow_control=True)   # recebe 1 e fica "em trânsito" sem ack
        with Producer(hub, [make_frame(50)], period=0.01):
            await asyncio.sleep(0.4)
            assert a.inflight == 1 and len(got_a) == 1
            hub.remove_client(a)                        # conexão cai
            b = hub.add_client(send_b, flow_control=True)   # reconecta (canal novo)
            assert b.inflight == 0
            await asyncio.sleep(0.4)
        a.ack(10**9)                                    # ack atrasado da conexão antiga: sem efeito em b
        assert b.inflight == 1
        await hub.stop()
        return got_a, got_b

    got_a, got_b = asyncio.run(main())
    assert len(got_a) == 1
    assert len(got_b) == 1                              # b recebeu logo, sem herdar o bloqueio de a


def test_pose_channel_is_latest_only_skips_ack_and_new_clients_get_current_pose():
    from backend.hub import encode_pose

    async def main():
        hub = Hub(fps=5)
        await hub.start()
        got, gate = [], asyncio.Event()

        async def send(msg):
            got.append(json.loads(msg))
            if got[-1]["type"] == "scan":
                await gate.wait()                           # cliente travado no envio da varredura

        hub.publish_pose((100.0, 200.0, 45.0))
        ch = hub.add_client(send, flow_control=True)        # já recebe a pose atual
        await asyncio.sleep(0.15)
        hub.publish(make_frame(10))
        await asyncio.sleep(0.3)                            # varredura enviada e SEM ack: o fluxo das varreduras trava
        for i in range(60):
            hub.publish_pose((100.0 + i, 200.0, 45.0))      # a pose continua chegando a ~30/s, uma por vez
            await asyncio.sleep(0.01)
        pending = ch._pose_msg is not None
        hub.publish_pose((100.0 + 59, 200.0, 45.0))         # igual à anterior: não repete
        gate.set()
        await asyncio.sleep(0.2)
        await hub.stop()
        return got, pending

    got, pending = asyncio.run(main())
    poses = [m for m in got if m["type"] == "pose"]
    assert poses[0]["pose"] == [100, 200, 45.0]
    assert poses[-1]["pose"] == [159, 200, 45.0]            # a mais nova; nunca fila
    assert len(poses) < 70                                  # no máximo uma mensagem por tick, sem acumular
    assert encode_pose((1.4, -2.6, 33.33)) == '{"type":"pose","pose":[1,-3,33.3]}'
