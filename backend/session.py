"""Dono da fonte ativa e do estado: disconnected -> connected -> scanning.

A leitura da fonte (bloqueante) roda numa thread própria; cada varredura é
filtrada/convertida pelo pipeline e publicada no Hub (que guarda só a última).

Transições válidas (as demais levantam SessionError com `code` e mensagem):
  disconnected --connect--> connected --start_scan--> scanning
  scanning --stop_scan--> connected          (ou queda da fonte: volta a connected + erro)
  connected|scanning --disconnect--> disconnected
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .config import POSE_FPS
from .hub import Hub
from .models import DeviceInfo, Health, Status
from .pipeline import scan_to_frame
from .sources.base import LidarSource, SourceError
from .sources.factory import create_source_for_port


log = logging.getLogger("lidar.session")

JOIN_TIMEOUT_S = 3.0      # espera pelo fim da thread de leitura ao parar


class SessionError(Exception):
    """Transição de estado inválida (ex.: varrer sem conectar)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Session:
    def __init__(self, hub: Hub,
                 source_for_port: Callable[[str], LidarSource] = create_source_for_port,
                 join_timeout: float = JOIN_TIMEOUT_S):
        self._hub = hub
        self._join_timeout = join_timeout
        self._factory = source_for_port
        self._lock = threading.RLock()
        self._source: LidarSource | None = None
        self._thread: threading.Thread | None = None
        self._state = "disconnected"
        self._port = ""
        self._info: DeviceInfo | None = None
        self._health: Health | None = None
        self._error = ""
        self.scan_count = 0
        self.last_points = 0

    # --- consulta ----------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    def status(self) -> Status:
        """Instantâneo SEM travar o lock: o painel consulta isto a cada 1 s e não pode ficar
        preso enquanto um connect() lento (timeout da serial) segura o lock."""
        source = self._source
        return Status(state=self._state, source=source.name if source else "",
                      port=self._port, info=self._info, health=self._health,
                      points=self.last_points,
                      errors=source.error_count if source else 0,
                      error=self._error)

    # --- comandos ------------------------------------------------------------------
    def connect(self, port: str) -> None:
        with self._lock:
            if self._state != "disconnected":
                raise SessionError("already_connected",
                                   f"já conectado a {self._port!r}; desconecte antes de conectar de novo")
            source = self._factory(port)
            try:
                source.open(port)
                self._info = source.get_info()
                self._health = source.get_health()
            except Exception as e:
                try:
                    source.close()
                except Exception:                    # não mascarar o erro original
                    log.exception("falha ao fechar a fonte após erro de abertura")
                self._info = self._health = None
                log.warning("connect(%s) falhou: %s", port, e)
                if not isinstance(e, SourceError):
                    raise SourceError(f"falha ao abrir {port}: {e}") from e
                raise
            self._source, self._port, self._error = source, port, ""
            self.scan_count = self.last_points = 0
            self._state = "connected"

    def start_scan(self) -> None:
        with self._lock:
            if self._state == "disconnected":
                raise SessionError("not_connected", "não há dispositivo conectado; conecte primeiro")
            if self._state == "scanning":
                raise SessionError("already_scanning", "a varredura já está em andamento")
            if self._thread is not None and self._thread.is_alive():
                raise SessionError("reader_busy", "a leitura anterior ainda não terminou "
                                   "(dispositivo travado?); aguarde ou desconecte")
            self._source.start_scan()
            self._error = ""
            self._thread = threading.Thread(target=self._read_loop, args=(self._source,),
                                            name="lidar-reader", daemon=True)
            self._state = "scanning"
            self._thread.start()
            if self._source.pose() is not None:     # só o rover simulado: posição a ~60/s, à parte das varreduras
                threading.Thread(target=self._pose_loop, args=(self._source, self._thread),
                                 name="lidar-pose", daemon=True).start()

    def steer(self, x: float, y: float, speed: float) -> None:
        source = self._source          # sem lock: comando de alta frequência, não pode esperar um connect()
        if source is None:
            raise SessionError("not_connected", "não há dispositivo conectado; conecte primeiro")
        source.steer(x, y, speed)

    def stop_scan(self) -> None:
        with self._lock:
            if self._state != "scanning":
                raise SessionError("not_scanning", "a varredura não está em andamento")
        self._halt_scan()

    def disconnect(self) -> None:
        with self._lock:
            if self._state == "disconnected":
                raise SessionError("already_disconnected", "já está desconectado")
        self.shutdown()

    def shutdown(self) -> None:
        """Encerra tudo, qualquer que seja o estado (idempotente; usado ao fechar o app)."""
        self._halt_scan()
        with self._lock:
            source, thread = self._source, self._thread
            self._source = self._thread = None      # a thread antiga deixa de publicar (ver _read_loop)
            if source is not None:
                try:
                    source.close()                  # fechar a porta também destrava leituras presas
                except Exception:
                    log.exception("falha ao fechar a fonte")
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                log.warning("thread de leitura não terminou após fechar a fonte (abandonada; é daemon)")
        with self._lock:
            self._info = self._health = None
            self._port = ""
            self._state = "disconnected"
            self.last_points = 0
        self._hub.clear()

    # --- interno ---------------------------------------------------------------------
    def _halt_scan(self) -> None:
        with self._lock:
            if self._state != "scanning":
                return
            self._source.stop_scan()
            thread = self._thread
        thread.join(timeout=self._join_timeout)
        with self._lock:
            if thread.is_alive():
                self._error = (f"a leitura não encerrou em {self._join_timeout:.0f} s "
                               f"(dispositivo travado?)")
                log.warning(self._error)
            if self._state == "scanning":
                self._state = "connected"
            self.last_points = 0
        self._hub.clear()      # sem varredura, ninguém recebe o último quadro "congelado"

    def _pose_loop(self, source: LidarSource, reader: threading.Thread) -> None:
        while self._thread is reader and self._state == "scanning":
            pose = source.pose()
            if pose is not None:
                self._hub.publish_pose(pose)
            time.sleep(1.0 / POSE_FPS)

    def _read_loop(self, source: LidarSource) -> None:
        error = ""
        try:
            for scan in source.scans():
                if self._thread is not threading.current_thread():
                    break                             # sessão encerrada/substituída: não publicar mais
                frame = scan_to_frame(scan)
                self._hub.publish(frame)
                self.last_points = len(frame.x)
                self.scan_count += 1
        except (SourceError, OSError) as e:
            error = str(e) or type(e).__name__
        except Exception as e:                        # bug inesperado: não deixar a thread morrer calada
            error = f"erro inesperado na leitura: {type(e).__name__}: {e}"
        finally:
            with self._lock:
                if self._thread is threading.current_thread() and self._state == "scanning":
                    self._state = "connected"
                    self._error = error
                    self.last_points = 0
            if error:
                log.warning("leitura encerrada com erro: %s", error)
                try:
                    source.stop_scan()                # a fonte não deve continuar marcada como varrendo
                except Exception:
                    log.exception("falha ao parar a fonte após erro")
                if self._thread is threading.current_thread():
                    self._hub.clear()
