"""Cliente de teste manual: imprime, a cada segundo, mensagens/s, pontos e saltos de seq.

Uso:
  python scripts/ws_client.py                     # cliente rápido
  python scripts/ws_client.py --delay 1.0         # cliente lento: leva 1 s por mensagem
Com --delay o `seq` salta (o servidor descarta quadros antigos) e a taxa cai,
mas o servidor não acumula fila: cada mensagem lida é a mais recente disponível
(atraso máximo: 1 mensagem em trânsito + 1 tick de 200 ms).
"""
import argparse
import json
import time

from websockets.sync.client import connect

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
ap.add_argument("--delay", type=float, default=0.0, help="atraso (s) após cada mensagem")
ap.add_argument("--seconds", type=float, default=0.0, help="encerra após N s (0 = até Ctrl+C)")
args = ap.parse_args()

with connect(args.url) as ws:
    start = window = time.monotonic()
    n = 0
    last_seq = None
    skipped = 0
    try:
        while not args.seconds or time.monotonic() - start < args.seconds:
            msg = json.loads(ws.recv())
            n += 1
            if last_seq is not None:
                skipped += msg["seq"] - last_seq - 1
            last_seq = msg["seq"]
            now = time.monotonic()
            if now - window >= 1.0:
                print(f"{n / (now - window):4.1f} msg/s | seq={msg['seq']:>5} | "
                      f"{len(msg['x'])} pontos | quadros pulados no período: {skipped}")
                window, n, skipped = now, 0, 0
            if args.delay:
                time.sleep(args.delay)
            ws.send(str(msg["seq"]))          # ack: controle de fluxo do servidor
    except KeyboardInterrupt:
        pass
