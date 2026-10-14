"""Hub de publicação: guarda só a varredura mais recente e a difunde a no máximo
`fps` mensagens por segundo, por WebSocket.

Garantias de memória/latência:
  * O hub retém UM quadro (o último publicado); publicar sobrescreve o anterior.
  * Cada cliente tem UM slot de mensagem pendente (fila de tamanho 1). Se o
    cliente é lento, a mensagem nova substitui a pendente; nunca se acumula fila.
  * O broadcaster nunca espera por um cliente: só deposita no slot.
  * Controle de fluxo opcional (`flow_control=True`): o cliente confirma (ack) o
    último `seq` processado e o servidor mantém no máximo `window` (padrão 1) mensagens em
    trânsito. Sem isso, os buffers TCP do sistema operacional enfileirariam
    mensagens antigas para um leitor lento (atraso crescente até encher o buffer).
    Se o ack não chega em `ack_timeout` s (aba oculta, cliente travado), o servidor
    considera o envio perdido e libera um novo: o fluxo nunca fica preso para sempre.
    O estado em trânsito é POR CONEXÃO: uma conexão nova começa zerada.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable

from .config import ACK_TIMEOUT_S, MAX_FPS
from .models import Frame

SendFn = Callable[[str], Awaitable[None]]


class ClientChannel:
    """Slot único + tarefa remetente de um cliente."""

    def __init__(self, send: SendFn, flow_control: bool = False, window: int = 1,
                 ack_timeout: float = ACK_TIMEOUT_S):
        self._send = send
        self._flow = flow_control
        self._window = window
        self._ack_timeout = ack_timeout
        self._sent_at = 0.0
        self._inflight: deque[int] = deque()      # seqs enviados e ainda sem ack
        self._slot: tuple[int, str] | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.sent = 0
        self.dropped = 0          # mensagens sobrescritas antes de serem enviadas
        self.ack_timeouts = 0     # envios liberados por falta de ack
        self.closed = False

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    @property
    def pending(self) -> int:
        """Mensagens retidas à espera deste cliente (0 ou 1, nunca mais)."""
        return 0 if self._slot is None else 1

    @property
    def inflight(self) -> int:
        return len(self._inflight)

    def offer(self, seq: int, msg: str) -> None:
        if self._slot is not None:
            self.dropped += 1
        self._slot = (seq, msg)
        self._wake.set()

    def ack(self, seq: int) -> None:
        """Cliente confirma que processou até `seq` (só tem efeito com flow_control)."""
        while self._inflight and self._inflight[0] <= seq:
            self._inflight.popleft()
        self._wake.set()

    async def _run(self) -> None:
        try:
            while not self.closed:
                timeout = None
                if self._flow and self._inflight:
                    timeout = max(0.0, self._sent_at + self._ack_timeout - time.monotonic())
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except asyncio.TimeoutError:
                    self._inflight.clear()      # ack nunca veio: libera novo envio
                    self.ack_timeouts += 1
                self._wake.clear()
                if self._slot is None:
                    continue
                if self._flow and len(self._inflight) >= self._window:
                    continue                # o slot espera; um ack acorda a tarefa
                (seq, msg), self._slot = self._slot, None
                if self._flow:
                    self._inflight.append(seq)
                    self._sent_at = time.monotonic()
                await self._send(msg)       # pode demorar à vontade: só este cliente espera
                self.sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            self.closed = True              # falha de envio: o hub remove o canal

    def close(self) -> None:
        self.closed = True
        self._slot = None
        if self._task is not None:
            self._task.cancel()


def encode_scan(seq: int, ts: float, frame: Frame) -> str:
    """JSON compacto: arrays x[], y[] (mm inteiros) e q[] (qualidade); sem objetos por ponto."""
    return json.dumps(
        {"type": "scan", "seq": seq, "t": round(ts, 3), "x": frame.x, "y": frame.y, "q": frame.q},
        separators=(",", ":"),
    )


class Hub:
    def __init__(self, fps: float = MAX_FPS, ack_timeout: float = ACK_TIMEOUT_S):
        self._period = 1.0 / fps
        self._ack_timeout = ack_timeout
        self._lock = threading.Lock()
        self._latest: tuple[int, float, Frame] | None = None
        self._seq = 0
        self._clients: set[ClientChannel] = set()
        self._task: asyncio.Task | None = None
        self.broadcasts = 0       # ticks em que algo foi difundido

    # --- lado produtor (qualquer thread) -------------------------------------
    def publish(self, frame: Frame) -> int:
        """Substitui o quadro mais recente. Seguro para chamar de outra thread."""
        with self._lock:
            self._seq += 1
            self._latest = (self._seq, time.time(), frame)
            return self._seq

    def clear(self) -> None:
        """Esquece o último quadro (varredura parada): ninguém recebe um quadro "congelado"."""
        with self._lock:
            self._latest = None

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    # --- lado clientes (loop asyncio) ------------------------------------------
    def add_client(self, send: SendFn, flow_control: bool = False) -> ClientChannel:
        ch = ClientChannel(send, flow_control=flow_control, ack_timeout=self._ack_timeout)
        ch.start()
        self._clients.add(ch)
        return ch

    def remove_client(self, ch: ClientChannel) -> None:
        ch.close()
        self._clients.discard(ch)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for ch in list(self._clients):
            self.remove_client(ch)

    async def _run(self) -> None:
        last_seq = 0
        last_tick = time.monotonic() - self._period     # primeiro tick imediato
        while True:
            wait = last_tick + self._period - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            # Espaçamento >= período entre ticks, mesmo se o loop atrasar (sem rajadas).
            last_tick = time.monotonic()

            for ch in [c for c in self._clients if c.closed]:
                self._clients.discard(ch)
            with self._lock:
                latest = self._latest
            if latest is None or latest[0] == last_seq or not self._clients:
                continue
            last_seq = latest[0]
            msg = encode_scan(*latest)                  # codifica uma vez para todos
            for ch in self._clients:
                ch.offer(latest[0], msg)
            self.broadcasts += 1
