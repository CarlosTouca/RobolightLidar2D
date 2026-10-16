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

from .config import ACK_TIMEOUT_S, MAX_FPS, POSE_FPS
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
        self._pose_msg: str | None = None         # pose pendente (slot único, sem ack): só a mais nova importa
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._pose_task: asyncio.Task | None = None
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

    def offer_pose(self, msg: str) -> None:
        self._pose_msg = msg
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
                if self._pose_msg is not None:      # a pose passa à frente do controle de fluxo das varreduras
                    msg, self._pose_msg = self._pose_msg, None
                    await self._send(msg)
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
    """JSON compacto: arrays x[], y[] (mm inteiros) e q[] (qualidade); sem objetos por ponto.
    `pose` ([x mm, y mm, rumo°]) só vai quando a fonte informa (rover simulado)."""
    msg = {"type": "scan", "seq": seq, "t": round(ts, 3), "x": frame.x, "y": frame.y, "q": frame.q}
    if frame.pose:
        px, py, heading = frame.pose
        msg["pose"] = [round(px), round(py), round(heading, 1)]
    return json.dumps(msg, separators=(",", ":"))


def encode_pose(pose: tuple[float, float, float]) -> str:
    """Posição do rover: {"type": "pose", "pose": [x mm, y mm, rumo°]}; vai a ~30/s, à parte das varreduras."""
    x, y, heading = pose
    return json.dumps({"type": "pose", "pose": [round(x), round(y), round(heading, 1)]}, separators=(",", ":"))


class Hub:
    def __init__(self, fps: float = MAX_FPS, ack_timeout: float = ACK_TIMEOUT_S):
        self._period = 1.0 / fps
        self._ack_timeout = ack_timeout
        self._lock = threading.Lock()
        self._latest: tuple[int, float, Frame] | None = None
        self._seq = 0
        self._pose: tuple[float, float, float] | None = None
        self._pose_seq = 0
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

    def publish_pose(self, pose: tuple[float, float, float]) -> None:
        """Última posição do rover (qualquer thread). Parado, não repete: o hub só difunde mudanças."""
        with self._lock:
            if pose != self._pose:
                self._pose = pose
                self._pose_seq += 1

    def clear(self) -> None:
        """Esquece o último quadro (varredura parada): ninguém recebe um quadro "congelado"."""
        with self._lock:
            self._latest = None
            self._pose = None

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    # --- lado clientes (loop asyncio) ------------------------------------------
    def add_client(self, send: SendFn, flow_control: bool = False) -> ClientChannel:
        ch = ClientChannel(send, flow_control=flow_control, ack_timeout=self._ack_timeout)
        ch.start()
        self._clients.add(ch)
        with self._lock:
            pose = self._pose
        if pose is not None:
            ch.offer_pose(encode_pose(pose))           # quem chega agora já recebe a posição atual
        return ch

    def remove_client(self, ch: ClientChannel) -> None:
        ch.close()
        self._clients.discard(ch)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        if self._task is None:
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run())
            self._pose_task = loop.create_task(self._run_pose())

    async def stop(self) -> None:
        for name in ("_task", "_pose_task"):
            task = getattr(self, name)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, name, None)
        for ch in list(self._clients):
            self.remove_client(ch)

    async def _run_pose(self) -> None:
        last = 0
        while True:
            await asyncio.sleep(1.0 / POSE_FPS)
            with self._lock:
                pose, seq = self._pose, self._pose_seq
            if pose is None or seq == last or not self._clients:
                continue
            last = seq
            msg = encode_pose(pose)
            for ch in self._clients:
                ch.offer_pose(msg)

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
