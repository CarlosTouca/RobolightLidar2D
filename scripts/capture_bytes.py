"""Grava os BYTES BRUTOS que o RPLIDAR C1 envia pela serial, para gerar fixtures de teste.

Roda com o sensor conectado; não depende do backend (os pedidos são os bytes literais do
manual de protocolo Slamtec v2.2, §4):
    STOP A5 25 | GET_INFO A5 50 | GET_HEALTH A5 52 | SCAN A5 20

Uso (Windows):
    .venv\\Scripts\\python scripts\\capture_bytes.py --list
    .venv\\Scripts\\python scripts\\capture_bytes.py --port COM9 --seconds 10

Gera em captures/ (um conjunto por execução, prefixo c1_AAAAMMDD_HHMMSS):
    <prefixo>_info.bin    resposta ao GET_INFO   (descritor 7 B + 20 B)
    <prefixo>_health.bin  resposta ao GET_HEALTH (descritor 7 B + 3 B)
    <prefixo>_scan.bin    tudo o que veio após o SCAN (descritor + fluxo de medidas)
    <prefixo>_meta.json   porta, baud, horários, contagens, SHA-256 e um resumo de sanidade

Para virar fixture de teste, copie os .bin para tests/fixtures/ (os nomes devem começar com
"c1_") e rode `pytest tests/test_rplidar_fixtures.py -v`.

O script sempre envia STOP ao terminar (o motor para). Não use portas Bluetooth: abrir uma
COM "Serial Padrão por link Bluetooth" pode iniciar uma conexão Bluetooth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BAUD = 460800                    # datasheet C1, "Communication Speed" / Fig. 2-8
STOP, GET_INFO, GET_HEALTH, SCAN = b"\xA5\x25", b"\xA5\x50", b"\xA5\x52", b"\xA5\x20"
DESC = b"\xA5\x5A"


def read_for(ser, seconds: float, until_len: int | None = None) -> bytes:
    """Lê por até `seconds`; se `until_len` é dado, para assim que houver esse tanto."""
    data = bytearray()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            data += chunk
        if until_len is not None and len(data) >= until_len:
            break
    return bytes(data)


def sanity(scan_bytes: bytes) -> dict:
    """Resumo mínimo e independente do backend: acha o descritor e confere os bits S/S̅/C."""
    i = scan_bytes.find(DESC)
    if i < 0 or len(scan_bytes) < i + 7:
        return {"descritor_encontrado": False}
    desc = scan_bytes[i:i + 7]
    body = scan_bytes[i + 7:]
    n = len(body) // 5
    valid = starts = 0
    for k in range(n):
        p = body[5 * k:5 * k + 5]
        s, sbar = p[0] & 1, (p[0] >> 1) & 1
        if s != sbar and (p[1] & 1) and (((p[2] << 7) | (p[1] >> 1)) < 360 * 64):
            valid += 1
            starts += s
    return {
        "descritor_encontrado": True,
        "descritor_hex": desc.hex(" "),
        "descritor_esperado_hex": "a5 5a 05 00 00 40 81",
        "bytes_apos_descritor": len(body),
        "pacotes_de_5_bytes": n,
        "pacotes_com_bits_validos": valid,
        "fracao_valida": round(valid / n, 4) if n else None,
        "inicios_de_volta_S1": starts,
    }


def capture_session(ser, seconds: float, out_dir: Path, port: str, log=print, do_scan: bool = True,
                    spinup: float = 5.0) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now().strftime("c1_%Y%m%d_%H%M%S")
    meta = {"porta": port, "baud": BAUD, "inicio": datetime.now().isoformat(timespec="seconds"),
            "pedidos": {"STOP": STOP.hex(" "), "GET_INFO": GET_INFO.hex(" "),
                        "GET_HEALTH": GET_HEALTH.hex(" "), "SCAN": SCAN.hex(" ")}, "arquivos": {}}

    def save(name: str, data: bytes):
        path = out_dir / f"{prefix}_{name}.bin"
        path.write_bytes(data)
        meta["arquivos"][path.name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        log(f"  {path.name}: {len(data)} bytes")

    try:
        log("STOP (estado conhecido)...")
        ser.write(STOP)
        ser.flush()
        time.sleep(0.05)
        ser.reset_input_buffer()

        log("GET_INFO...")
        ser.write(GET_INFO)
        ser.flush()
        info = read_for(ser, 2.0, until_len=27)
        save("info", info)
        meta["info_ok"] = len(info) == 27 and info.startswith(DESC)

        log("GET_HEALTH...")
        ser.write(GET_HEALTH)
        ser.flush()
        health = read_for(ser, 2.0, until_len=10)
        save("health", health)
        meta["health_ok"] = len(health) == 10 and health.startswith(DESC)

        if do_scan:
            log(f"SCAN por {seconds:.0f} s (o motor precisa estabilizar antes dos dados)...")
            ser.reset_input_buffer()
            ser.write(SCAN)
            ser.flush()
            t0 = time.monotonic()
            scan = read_for(ser, seconds + spinup)   # +5 s: partida do motor ([P22] p.16)
            meta["scan_segundos"] = round(time.monotonic() - t0, 2)
            save("scan", scan)
            meta["scan_sanidade"] = sanity(scan)
            log("resumo: " + json.dumps(meta["scan_sanidade"], ensure_ascii=False))
    finally:
        try:
            ser.write(STOP)
            ser.flush()
            time.sleep(0.01)
        except Exception as e:                    # cabo removido etc.: não esconder o que já foi gravado
            log(f"aviso: não foi possível enviar STOP final ({e})")
        meta["fim"] = datetime.now().isoformat(timespec="seconds")
        (out_dir / f"{prefix}_meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
        log(f"  {prefix}_meta.json")
    return meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="porta serial do C1 (ex.: COM9)")
    ap.add_argument("--seconds", type=float, default=10.0, help="duração da captura de varredura (s)")
    ap.add_argument("--out-dir", default="captures", help="pasta de saída (padrão: captures)")
    ap.add_argument("--no-scan", action="store_true", help="só GET_INFO e GET_HEALTH (sem ligar o motor)")
    ap.add_argument("--list", action="store_true", help="lista as portas seriais e sai")
    args = ap.parse_args()

    import serial
    from serial.tools import list_ports

    if args.list:
        for p in sorted(list_ports.comports(), key=lambda p: p.device):
            usb = f" [USB {p.vid:04X}:{p.pid:04X}]" if p.vid is not None else ""
            print(f"{p.device} — {p.description}{usb}")
        return 0
    if not args.port:
        ap.error("informe --port (use --list para ver as portas)")

    try:
        ser = serial.Serial(args.port, BAUD, bytesize=8, parity="N", stopbits=1, timeout=0.1, write_timeout=1.0)
    except (OSError, ValueError) as e:
        print(f"erro: não foi possível abrir {args.port}: {e}\n"
              f"(porta ocupada por outro programa? cabo/adaptador desconectado?)", file=sys.stderr)
        return 1
    try:
        meta = capture_session(ser, args.seconds, Path(args.out_dir), args.port, do_scan=not args.no_scan)
    except KeyboardInterrupt:
        print("interrompido; arquivos parciais foram gravados.")
        return 130
    except (OSError, serial.SerialException) as e:
        print(f"erro de comunicação: {e} (cabo desconectado?)", file=sys.stderr)
        return 1
    finally:
        ser.close()
    ok = meta.get("info_ok") and meta.get("health_ok") and (
        args.no_scan or meta.get("scan_sanidade", {}).get("descritor_encontrado"))
    print("OK" if ok else "ATENÇÃO: faltou resposta do sensor; veja o resumo acima e o _meta.json.")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
