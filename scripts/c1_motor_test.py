"""TESTE DE BANCADA: o RPLIDAR C1 aceita mudar a velocidade do motor? (EXPERIMENTAL, NÃO validado)

O datasheet do C1 (rev.1.1, p.14, "Scanner Motor Control") e o manual do kit (rev.1.0, p.12) dizem que a
velocidade pode ser mudada por comando (faixa 8 a 12 Hz = 480 a 720 rpm, p.9). O SDK público, porém, não
usa esse comando no C1, e a nota de rodapé do MOTOR_SPEED_CTRL no protocolo v2.8 (p.32) não pôde ser lida.
Este script responde à pergunta no SEU aparelho, sem chutar:

  1. Leitura (padrão, não muda nada): STOP, GET_INFO, GET_HEALTH, GET_LIDAR_CONF das entradas de rotação
     (desejada/mínima/máxima: só existem no SDK, o firmware pode não responder), SCAN e medição da rotação
     pelo intervalo entre voltas (S = 1), conforme o protocolo v2.8 p.34. Em seguida STOP.
  2. Mudança (só com --set-rpm E --i-understand): depois da leitura, envia  A5 A8 02 <rpm u16> <checksum>
     (MOTOR_SPEED_CTRL, v2.8 p.32) para cada rpm pedido, espera estabilizar, mede de novo e diz se o motor
     seguiu o pedido. No fim SEMPRE restaura --restore-rpm (padrão 600 = 10 Hz) e dá STOP.

Limites de segurança: só aceita 480 a 720 rpm (e, se o sensor informar mínimo/máximo, só dentro deles).
Rpm fora disso é recusado antes de abrir a porta. Mudanças grandes não são feitas de uma vez: comece com
valores próximos, por exemplo  --set-rpm 540 660.

Uso (Windows, sensor C1 ligado por USB com a alimentação do kit):
    .venv\\Scripts\\python scripts\\c1_motor_test.py --list
    .venv\\Scripts\\python scripts\\c1_motor_test.py --port COM9                          # só leitura
    .venv\\Scripts\\python scripts\\c1_motor_test.py --port COM9 --set-rpm 540 660 --i-understand

Grava captures/c1_motor_AAAAMMDD_HHMMSS.json (bytes brutos das respostas e rotações medidas); mande esse
arquivo para fecharmos o README ("Velocidade do motor"). NÃO use as portas "Serial Padrão por link
Bluetooth" (COM3 a COM8 neste PC): abrir uma delas pode iniciar uma conexão Bluetooth. O script recusa
portas Bluetooth.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.sources.base import SourceError  # noqa: E402
from backend.sources.rplidar_c1 import RplidarC1  # noqa: E402
from backend.sources.rplidar_protocol import (  # noqa: E402
    C1_MAX_RPM, C1_MIN_RPM, CONF_DESIRED_ROT_FREQ, CONF_MAX_ROT_FREQ, CONF_MIN_ROT_FREQ,
)

SETTLE_S = 3.0          # espera depois de mandar uma rotação nova, antes de medir
FOLLOW_TOL = 0.05       # "seguiu o pedido": medido dentro de 5% do alvo
IGNORE_TOL = 0.03       # "ignorou": medido dentro de 3% da rotação inicial, apesar de um pedido bem diferente
MIN_CHANGE = 0.08       # pedido "bem diferente": pelo menos 8% da rotação inicial


def rpm_from_times(times: list[float]) -> float:
    """Rotação pelo intervalo entre voltas completas (protocolo v2.8 p.34: RPM = 60 / ΔT)."""
    if len(times) < 3:
        raise ValueError(f"poucas voltas para medir a rotação: {len(times)}")
    return 60.0 * (len(times) - 1) / (times[-1] - times[0])


def measure_rpm(drv, seconds: float) -> tuple[float, int]:
    times: list[float] = []
    end = time.monotonic() + seconds
    for _ in drv.scans():
        times.append(time.monotonic())
        if time.monotonic() >= end:
            break
    return rpm_from_times(times), len(times)


def check_targets(targets: list[int], limits: dict | None = None) -> list[int]:
    """Recusa rpm fora de 480-720 (datasheet) ou fora do mínimo/máximo que o próprio sensor informou."""
    lo, hi = C1_MIN_RPM, C1_MAX_RPM
    sensor_min = (limits or {}).get("min", {}).get("rpm")
    sensor_max = (limits or {}).get("max", {}).get("rpm")
    if sensor_min:                                               # 0 ou ausente = o sensor não informou
        lo = max(lo, sensor_min)
    if sensor_max:
        hi = min(hi, sensor_max)
    for t in targets:
        if not lo <= t <= hi:
            raise ValueError(f"rpm {t} fora da faixa permitida ({lo}-{hi} rpm)")
    return targets


def verdict(baseline: float, results: list[dict]) -> str:
    seguiu = [r for r in results if abs(r["medido_rpm"] - r["alvo_rpm"]) <= FOLLOW_TOL * r["alvo_rpm"]]
    ignorou = [r for r in results
               if abs(r["alvo_rpm"] - baseline) >= MIN_CHANGE * baseline
               and abs(r["medido_rpm"] - baseline) <= IGNORE_TOL * baseline]
    if results and len(seguiu) == len(results):
        return "SEGUIU: o motor acompanhou todos os pedidos (o C1 aceita MOTOR_SPEED_CTRL)."
    if results and len(ignorou) == len(results):
        return "IGNOROU: a rotação não mudou (o firmware deste C1 não aceita o comando, ou ele não vale aqui)."
    return "INCONCLUSIVO: resultados mistos; veja o JSON (pode ser a alimentação, o aquecimento ou um valor fora do alcance)."


def read_limits(drv) -> dict:
    out = {}
    for name, entry in (("desired", CONF_DESIRED_ROT_FREQ), ("min", CONF_MIN_ROT_FREQ), ("max", CONF_MAX_ROT_FREQ)):
        try:
            raw = drv.read_conf(entry)
            out[name] = {"hex": raw.hex(" "), "rpm": struct.unpack_from("<H", raw)[0] if len(raw) >= 2 else None}
        except SourceError as e:
            out[name] = {"erro": str(e)}
            try:                                                 # descarta o que sobrou de uma resposta que não veio inteira
                drv._ser.reset_input_buffer()
            except Exception:
                pass
    return out


def run(drv, port: str, targets: list[int], seconds: float, restore_rpm: int,
        measure=measure_rpm, sleep=time.sleep) -> dict:
    """Roda o teste com um driver já aberto. `measure` e `sleep` são injetáveis para os testes."""
    report: dict = {"porta": port, "inicio": datetime.now().isoformat(timespec="seconds"),
                    "info": vars(drv.get_info()), "saude": str(drv.get_health().status.value),
                    "limites": read_limits(drv), "alvos": targets, "mudancas": []}
    check_targets(targets + ([restore_rpm] if targets else []), report["limites"])
    sent_any = False
    try:
        drv.start_scan()
        sleep(SETTLE_S)                                          # o motor precisa estabilizar ([P22] p.16)
        base, n = measure(drv, seconds)
        report["rotacao_inicial"] = {"rpm": round(base, 1), "hz": round(base / 60, 2), "voltas": n}
        for t in targets:
            drv.set_motor_rpm(t)
            sent_any = True
            sleep(SETTLE_S)
            try:
                got, n = measure(drv, seconds)
                report["mudancas"].append({"alvo_rpm": t, "medido_rpm": round(got, 1), "voltas": n})
            except (ValueError, SourceError) as e:
                report["mudancas"].append({"alvo_rpm": t, "medido_rpm": None, "erro": str(e)})
        report["veredito"] = verdict(base, [m for m in report["mudancas"] if m.get("medido_rpm")]) if targets else \
            "SÓ LEITURA: nada foi mudado."
    finally:
        if sent_any:                                             # nunca deixa o motor numa rotação de teste
            try:
                drv.set_motor_rpm(restore_rpm)
                sleep(SETTLE_S)
                got, n = measure(drv, seconds)
                report["restaurado"] = {"alvo_rpm": restore_rpm, "medido_rpm": round(got, 1), "voltas": n}
            except Exception as e:                               # noqa: BLE001 - é a limpeza; registra e segue
                report["restaurado"] = {"alvo_rpm": restore_rpm, "erro": str(e)}
        try:
            drv.stop_scan()
        except Exception:
            pass
    return report


def refuse_bluetooth(port: str) -> None:
    from serial.tools import list_ports
    known = {p.device: p for p in list_ports.comports()}
    if port not in known:
        sys.exit(f"porta {port} não existe. Portas: {', '.join(sorted(known)) or 'nenhuma'}")
    desc = (known[port].description or "") + " " + (known[port].hwid or "")
    if "bluetooth" in desc.lower() or "bthenum" in desc.lower():
        sys.exit(f"{port} é uma porta Bluetooth ({desc.strip()}): abri-la pode iniciar uma conexão. Use a porta USB do C1.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--list", action="store_true", help="lista as portas e sai")
    ap.add_argument("--port", help="porta USB do C1, ex.: COM9")
    ap.add_argument("--seconds", type=float, default=6.0, help="janela de cada medição (padrão 6 s)")
    ap.add_argument("--set-rpm", type=int, nargs="+", default=[], metavar="RPM", help="rotações a testar (480-720)")
    ap.add_argument("--restore-rpm", type=int, default=600, help="rotação restaurada no fim (padrão 600 = 10 Hz)")
    ap.add_argument("--i-understand", action="store_true", help="confirma que o motor vai mudar de velocidade")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "captures"))
    args = ap.parse_args(argv)

    if args.list:
        from serial.tools import list_ports
        for p in sorted(list_ports.comports(), key=lambda p: p.device):
            print(f"{p.device:8} {p.description}  [{p.hwid}]")
        return 0
    if not args.port:
        ap.error("--port é obrigatório (use --list para ver as portas)")
    if args.set_rpm and not args.i_understand:
        ap.error("--set-rpm muda a velocidade do motor: confirme com --i-understand")
    try:
        check_targets(args.set_rpm + ([args.restore_rpm] if args.set_rpm else []))
    except ValueError as e:
        ap.error(str(e))
    refuse_bluetooth(args.port)

    drv = RplidarC1()
    drv.open(args.port)
    try:
        report = run(drv, args.port, args.set_rpm, args.seconds, args.restore_rpm)
    finally:
        drv.close()

    out = Path(args.out_dir) / f"c1_motor_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n{report['veredito']}\nRelatório salvo em {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
