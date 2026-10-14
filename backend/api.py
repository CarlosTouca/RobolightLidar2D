"""Rotas: controle REST + WebSocket de varreduras.

REST (JSON). Erros têm sempre o formato {"error": {"code": "...", "message": "..."}}:
  GET  /api/config        padrões da interface (alcance dos eixos, taxa máxima)
  GET  /api/ports         portas disponíveis (SIMULADOR sempre presente)
  GET  /api/status        estado, dispositivo, saúde, pontos, erro da fonte
  POST /api/connect       {"port": "SIMULADOR" | "COMx"}
  POST /api/disconnect
  POST /api/scan/start
  POST /api/scan/stop
  POST /api/drive         {"left": -1..1, "right": -1..1}  rover simulado (só o SIMULADOR reage)
  409 transição inválida (ex.: iniciar varredura sem conectar) | 502 falha da fonte

WebSocket /ws: o cliente envia, a cada varredura desenhada, o seu `seq` como texto
(ex.: "42"); o servidor mantém no máximo 1 varredura sem ack em trânsito.
"""
from dataclasses import asdict

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config
from .session import Session, SessionError
from .sources.base import SourceError
from .sources.factory import list_ports

router = APIRouter()


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def session_error_handler(request: Request, exc: SessionError) -> JSONResponse:
    return error_response(409, exc.code, exc.message)


async def source_error_handler(request: Request, exc: SourceError) -> JSONResponse:
    return error_response(502, "source_error", str(exc))


def _session(request: Request) -> Session:
    return request.app.state.session


def _status(request: Request) -> dict:
    return asdict(_session(request).status())


class ConnectBody(BaseModel):
    port: str


class DriveBody(BaseModel):
    left: float = Field(ge=-1, le=1)
    right: float = Field(ge=-1, le=1)


@router.post("/api/drive")
def drive(body: DriveBody, request: Request) -> dict:
    _session(request).drive(body.left, body.right)
    return {"ok": True}


@router.get("/api/config")
def get_config() -> dict:
    return {"default_range_m": config.DEFAULT_RANGE_M, "max_fps": config.MAX_FPS,
            "baudrate": config.BAUDRATE}


@router.get("/api/ports")
def get_ports() -> dict:
    return {"ports": [asdict(p) for p in list_ports()]}


@router.get("/api/status")
def get_status(request: Request) -> dict:
    return _status(request)


@router.post("/api/connect")
def connect(body: ConnectBody, request: Request) -> dict:
    _session(request).connect(body.port)
    return _status(request)


@router.post("/api/disconnect")
def disconnect(request: Request) -> dict:
    _session(request).disconnect()
    return _status(request)


@router.post("/api/scan/start")
def scan_start(request: Request) -> dict:
    _session(request).start_scan()
    return _status(request)


@router.post("/api/scan/stop")
def scan_stop(request: Request) -> dict:
    _session(request).stop_scan()
    return _status(request)


@router.websocket("/ws")
async def ws_scans(websocket: WebSocket) -> None:
    hub = websocket.app.state.hub
    await websocket.accept()
    channel = hub.add_client(websocket.send_text, flow_control=True)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            # ack: último seq processado. Qualquer outra coisa (binário, texto estranho, números
            # gigantes) é ignorada: um cliente defeituoso nunca derruba a conexão nem o servidor.
            text = message.get("text")
            if text and len(text) <= 12 and text.isascii() and text.isdigit():
                channel.ack(int(text))
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove_client(channel)
