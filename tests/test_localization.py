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
    scene, rng, est = house_scene(), np.random.default_rng(0), PoseEstimator()
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
    scene, rng, est = house_scene(), np.random.default_rng(1), PoseEstimator()
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
    scene, rng, est = house_scene(), np.random.default_rng(2), PoseEstimator()
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
        assert s.status().pose_mode == "estimada"
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
