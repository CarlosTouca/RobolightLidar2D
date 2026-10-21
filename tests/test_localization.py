"""Estimativa de posição só pelo LiDAR (ICP). Verdade conhecida no simulador; dados reais do C1 parado."""
import math
from pathlib import Path

import numpy as np
import pytest

from backend.localization import PoseEstimator, surface_normals
from backend.pipeline import scan_to_frame
from backend.sources.rplidar_protocol import StandardScanDecoder, find_descriptor
from backend.sources.simulator import house_scene, simulate_scan

FIXTURE = Path(__file__).parent / "fixtures" / "c1_20261009_174436_scan.bin"


def trajectory(step=80.0, turn=5.0):
    """Poses (x mm, y mm, rumo°) pela casa: sala -> abertura -> corredor -> à esquerda. Giros de até 5° por varredura."""
    x = y = h = 0.0
    out = [(x, y, h)]

    def turn_to(target):
        nonlocal h
        while abs(((target - h + 180) % 360) - 180) > 1e-6:
            d = ((target - h + 180) % 360) - 180
            h = (h + max(-turn, min(turn, d))) % 360
            out.append((x, y, h))

    def go(dist):
        nonlocal x, y
        for _ in range(int(dist // step)):
            x += math.sin(math.radians(h)) * step
            y += math.cos(math.radians(h)) * step
            out.append((x, y, h))

    turn_to(90); go(1500); turn_to(0); go(1800); turn_to(270); go(2500)
    return out


def heading_error(a, b):
    return abs(((a - b + 180) % 360) - 180)


def test_surface_normals_of_a_straight_wall_point_across_it():
    wall = np.column_stack([np.linspace(-1000, 1000, 40), np.full(40, 2000.0)])      # parede reta em y = 2000
    n, ok = surface_normals(wall)
    assert ok[1:-1].all() and np.allclose(np.abs(n[1:-1, 1]), 1.0, atol=1e-6) and np.allclose(n[1:-1, 0], 0, atol=1e-6)
    gap = np.array([[0, 1000.0], [10, 1000.0], [900, 1000.0]])                        # salto grande = borda de objeto
    assert not surface_normals(gap)[1][1]


def test_pose_follows_the_true_path_through_the_simulated_house():
    scene, rng, est = house_scene(), np.random.default_rng(0), PoseEstimator(min_range_mm=300)
    path, lost, worst = trajectory(), 0, 0.0
    for p in path:
        f = scan_to_frame(simulate_scan(scene, rng, pose=p))                          # ruído, perdas e jitter do simulador
        e, ok = est.update(f.x, f.y)
        lost += not ok
        worst = max(worst, math.hypot(e[0] - p[0], e[1] - p[1]))
        assert heading_error(e[2], p[2]) < 4.0
    assert lost <= 5 and worst < 150.0                                                # medido: ~50 mm de pior erro em ~6 m
    assert math.hypot(e[0] - p[0], e[1] - p[1]) < 120.0


def test_stationary_real_c1_capture_does_not_drift():
    if not FIXTURE.exists():
        pytest.skip("captura real do C1 ausente")
    buf = bytearray(FIXTURE.read_bytes())
    find_descriptor(buf)
    est, lost, far = PoseEstimator(), 0, 0.0
    for scan in StandardScanDecoder().feed(bytes(buf)):
        f = scan_to_frame(scan)
        e, ok = est.update(f.x, f.y)
        lost += not ok
        far = max(far, math.hypot(e[0], e[1]))
    assert lost == 0 and far < 15.0                                                   # medido: 3,4 mm em 128 voltas reais


def test_unmatchable_scan_keeps_the_pose_and_reports_lost():
    scene, rng, est = house_scene(), np.random.default_rng(1), PoseEstimator(min_range_mm=300)
    f = scan_to_frame(simulate_scan(scene, rng))
    est.update(f.x, f.y)
    est.update(f.x, f.y)
    before = est.pose
    noise = rng.uniform(-3000, 3000, (300, 2))                                       # nada a ver com a sala
    e, ok = est.update(noise[:, 0], noise[:, 1])
    assert not ok and e == before
    e, ok = est.update(f.x[:10], f.y[:10])                                           # quase nenhum ponto
    assert not ok and e == before


def test_reset_goes_back_to_the_origin():
    scene, rng, est = house_scene(), np.random.default_rng(2), PoseEstimator(min_range_mm=300)
    for p in trajectory()[:40]:
        f = scan_to_frame(simulate_scan(scene, rng, pose=p))
        est.update(f.x, f.y)
    assert est.pose != (0.0, 0.0, 0.0)
    est.reset()
    assert est.pose == (0.0, 0.0, 0.0) and est.ok


def test_session_estimates_the_pose_for_a_source_that_has_none():
    import dataclasses
    import time

    from backend.hub import Hub
    from backend.session import Session, SessionError
    from backend.sources.simulator import Simulator

    class NoPose(Simulator):                      # como o C1 real: entrega varreduras, não a posição
        def pose(self):
            return None

        def scans(self):
            for s in super().scans():
                yield dataclasses.replace(s, pose=None)

    sim = NoPose(seed=0)
    hub = Hub()
    s = Session(hub, source_for_port=lambda p: sim)
    s.connect("SIMULADOR")
    try:
        assert s.status().pose_mode == "estimada" and s.status().pose_blind_m == pytest.approx(0.8)
        s.set_blind_zone(0.3)                     # a parede está a menos de 0,8 m do ponto final
        assert s.status().pose_blind_m == pytest.approx(0.3)
        s.start_scan()
        t_end = time.monotonic() + 2.0
        while time.monotonic() < t_end:           # anda para a frente (e renova o comando, senão o rover para sozinho)
            sim.steer(0, 1, 1.0)
            time.sleep(0.1)
        time.sleep(1.5)                           # para e deixa as últimas varreduras chegarem
        frame = hub._latest[2]
        truth = sim._pose
        assert frame.pose is not None and truth[1] > 500                       # andou de fato (até a parede do corredor, em y = 750)
        assert math.hypot(frame.pose[0] - truth[0], frame.pose[1] - truth[1]) < 150
        assert heading_error(frame.pose[2], truth[2]) < 4 and s.status().pose_ok
        assert hub._pose is not None                                            # e a seta recebe a mesma posição
        s.reset_pose()
        assert s._estimator.pose == (0.0, 0.0, 0.0)
    finally:
        s.shutdown()
    with pytest.raises(SessionError):
        s.reset_pose()                                                          # desconectado: não há estimador


def test_sources_that_report_their_own_pose_are_not_estimated(tmp_path):
    from fastapi.testclient import TestClient

    from backend.main import create_app
    with TestClient(create_app()) as c:
        assert c.post("/api/pose/reset").status_code == 409                    # sem conexão
        c.post("/api/connect", json={"port": "SIMULADOR"})
        st = c.get("/api/status").json()
        assert st["pose_mode"] == "fonte" and st["pose_ok"] is True
        r = c.post("/api/pose/reset")
        assert r.status_code == 409 and "já informa a posição" in r.json()["error"]["message"]


def test_points_attached_to_the_rover_fool_the_match_unless_the_blind_zone_covers_them():
    scene, path = house_scene(), trajectory()
    ang = np.radians(np.linspace(200, 330, 60))
    rad = np.linspace(500, 750, 60)                                    # mastro/cabos/bateria: andam junto com o sensor
    fixed = (np.sin(ang) * rad, np.cos(ang) * rad)

    def worst_error(min_range_mm):
        rng, est, worst = np.random.default_rng(0), PoseEstimator(min_range_mm=min_range_mm), 0.0
        for p in path:
            f = scan_to_frame(simulate_scan(scene, rng, pose=p))
            e, _ = est.update(np.concatenate([f.x, fixed[0]]), np.concatenate([f.y, fixed[1]]))
            worst = max(worst, math.hypot(e[0] - p[0], e[1] - p[1]))
        return worst

    assert worst_error(300) > 1000.0                                   # sem zona cega: acha que o rover quase não andou
    assert worst_error(800) < 150.0                                    # com a zona cega padrão: acompanha o caminho


def test_recorder_keeps_the_latest_scans_and_writes_ndjson():
    import json

    from backend.models import Frame
    from backend.recorder import ScanRecorder

    rec = ScanRecorder(max_scans=3)
    for i in range(5):
        rec.add(float(i), Frame(x=[i, i + 1], y=[-i, 5], q=[10, 20]), (i * 10.0, 0.0, 90.0), i % 2 == 0)
    rows = [json.loads(line) for line in rec.lines()]
    assert rows[0]["formato"] == 1 and len(rows) == 4                     # cabeçalho + as 3 últimas
    assert [r["t"] for r in rows[1:]] == [2.0, 3.0, 4.0] and rows[1]["x"] == [2, 3] and rows[1]["y"] == [-2, 5]
    assert rows[3]["pose"] == [40.0, 0.0, 90.0] and rows[3]["ok"] is True and rows[2]["ok"] is False


def test_replay_script_reads_a_recording_and_reports(tmp_path, capsys):
    import importlib.util
    import json

    spec = importlib.util.spec_from_file_location("replay_scans", Path(__file__).parent.parent / "scripts" / "replay_scans.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)
    scene, rng = house_scene(), np.random.default_rng(3)
    lines = [json.dumps({"formato": 1, "unidade": "mm"})]
    for p in trajectory()[:30]:
        f = scan_to_frame(simulate_scan(scene, rng, pose=p))
        lines.append(json.dumps({"t": 0, "x": f.x, "y": f.y, "q": f.q, "pose": None, "ok": True}))
    path = tmp_path / "v.ndjson"
    path.write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    assert rs.main([str(path), "--min-range", "0.3", "--png", str(tmp_path / "m.png")]) == 0
    out = capsys.readouterr().out
    assert "30 varreduras" in out and "replay com zona cega" in out and (tmp_path / "m.png").exists()


def test_blind_zone_and_recording_endpoints():
    from fastapi.testclient import TestClient

    from backend.main import create_app
    with TestClient(create_app()) as c:
        assert c.post("/api/pose/blind", json={"m": 1.0}).status_code == 409           # sem conexão
        c.post("/api/connect", json={"port": "SIMULADOR"})
        assert c.post("/api/pose/blind", json={"m": 1.0}).status_code == 409           # o simulador já informa a posição
        assert c.post("/api/pose/blind", json={"m": 9}).status_code == 422
        r = c.get("/api/recording")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
        assert "attachment" in r.headers["content-disposition"] and r.text.startswith('{"formato"')
