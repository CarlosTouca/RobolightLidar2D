"""Configuração central. Valores podem ser sobrescritos por variáveis de ambiente."""
import os

BAUDRATE = 460800                      # RPLIDAR C1
ACK_TIMEOUT_S = 2.0                    # sem ack por este tempo, o servidor libera novo envio
MAX_FPS = 5                            # limite de envio ao navegador
HOST = os.getenv("LIDAR_HOST", "127.0.0.1")
PORT = int(os.getenv("LIDAR_PORT", "8000"))
DEFAULT_RANGE_M = float(os.getenv("LIDAR_RANGE_M", "6"))   # eixos ±N metros
