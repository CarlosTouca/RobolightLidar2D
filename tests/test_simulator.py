import math
import time

import numpy as np
import pytest

from backend.pipeline import scan_to_frame
from backend.sources.base import SourceError
from backend.sources.simulator import (
    Scene, Simulator, default_scene, raycast, rect_segments, simulate_scan, u_shape,
)

CLEAN = dict(noise_mm=0.0, angle_jitter_deg=0.0, dropout=0.0)
ROOM = rect_segments(-2500, -2000, 3500, 3000)   # sensor na origem


def test_walls_exact_distance():
    d = raycast(Scene(segments=ROOM), np.array([0.0, 90.0, 180.0, 270.0]))
    assert d == pytest.approx([3000, 3500, 2000, 2500])   # frente, direita, trás, esquerda


def test_wall_diagonal_distance():
    d = raycast(Scene(segments=ROOM), np.array([45.0]))
    assert d[0] == pytest.approx(3000 / math.cos(math.radians(45)))


def test_circle_hit_and_miss():
    scene = Scene(circles=[(0.0, 2000.0, 300.0)])          # à frente
    d = raycast(scene, np.array([0.0, 90.0, 180.0]))
    assert d[0] == pytest.approx(1700)
    assert np.isinf(d[1]) and np.isinf(d[2])
    tang = math.degrees(math.asin(300 / 2000))              # ângulo tangente
    d = raycast(scene, np.array([tang * 0.99, tang * 1.01]))
    assert np.isfinite(d[0]) and np.isinf(d[1])


def test_u_shape_concave_geometry():
    w, h, t, x0 = 600.0, 500.0, 100.0, 1000.0
    # U girado 180°: abertura voltada para -X (para o sensor); fundo em x0+w-t..x0+w,
    # pontas dos braços em x = x0. Centro em (x0+w/2, 0).
    scene = Scene(segments=u_shape((x0 + w / 2, 0.0), w, h, t, rot_deg=180.0))

    def ang(X, Y):   # ângulo a partir de +Y, horário, do ponto (X, Y)
        return math.degrees(math.atan2(X, Y))

    # Pelo meio da abertura vê-se a face interna do fundo, em x = x0 + w - t
    assert raycast(scene, np.array([90.0]))[0] == pytest.approx(x0 + w - t)

    # Na altura do braço inferior bate na ponta do braço, x = x0
    Y = -h / 2 + t / 2
    d = raycast(scene, np.array([ang(x0, Y)]))[0]
    assert d == pytest.approx(math.hypot(x0, Y))

    # Logo acima do braço (dentro da cavidade) passa pela ponta e bate no fundo
    # interno (x = x0 + w - t), bem mais longe que a ponta do braço
    Y2 = -h / 2 + t + 1
    d = raycast(scene, np.array([ang(x0 + w - t, Y2)]))[0]
    assert d == pytest.approx(math.hypot(x0 + w - t, Y2), rel=1e-6)
    assert d > math.hypot(x0, Y2) + 400

    # Por fora, de lado a lado, o fundo externo (x0 + w) fica escondido: nada além
    assert raycast(scene, np.array([ang(x0 + w + 50, 0.0)]))[0] == pytest.approx(x0 + w - t, abs=1)


def test_default_scene_shows_circle_and_u():
    scene = default_scene()
    scan = simulate_scan(scene, np.random.default_rng(0), **CLEAN)
    assert len(scan.angle_deg) == 500
    assert np.all(scan.distance_mm > 0) and np.all(scan.quality > 0)
    assert scan.distance_mm.max() < math.hypot(3500, 3000) + 1   # nada além de um canto
    walls = simulate_scan(Scene(segments=ROOM), np.random.default_rng(0), **CLEAN)
    assert (scan.distance_mm < walls.distance_mm - 100).sum() > 10


def test_noise_statistics():
    scene = default_scene()
    clean = simulate_scan(scene, np.random.default_rng(1), **CLEAN)
    noisy = [simulate_scan(scene, np.random.default_rng(i), noise_mm=10.0,
                           angle_jitter_deg=0.0, dropout=0.0) for i in range(200)]
    err = np.concatenate([s.distance_mm - clean.distance_mm for s in noisy])
    assert abs(err.mean()) < 1.0
    assert err.std() == pytest.approx(10.0, rel=0.05)


def test_dropout_rate_and_filter():
    scene = default_scene()
    rng = np.random.default_rng(3)
    total = invalid = 0
    for _ in range(100):
        s = simulate_scan(scene, rng, dropout=0.05)
        total += len(s.quality)
        invalid += int((s.quality == 0).sum())
        assert np.all((s.quality == 0) == (s.distance_mm == 0))   # sempre juntos
        assert len(scan_to_frame(s).x) == (s.quality > 0).sum()
    assert invalid / total == pytest.approx(0.05, abs=0.01)


def test_quality_range_and_decreases_with_distance():
    s = simulate_scan(default_scene(), np.random.default_rng(4), **CLEAN)
    assert s.quality.min() >= 1 and s.quality.max() <= 63
    med = np.median(s.distance_mm)
    near, far = s.distance_mm < med, s.distance_mm >= med
    assert s.quality[near].mean() > s.quality[far].mean()


def test_beyond_max_range_is_invalid():
    scene = Scene(segments=rect_segments(-20000, -20000, 20000, 20000))
    s = simulate_scan(scene, np.random.default_rng(0), **CLEAN)
    assert np.all(s.distance_mm == 0) and np.all(s.quality == 0)


def test_simulator_lifecycle_and_stream():
    sim = Simulator(seed=0)
    with pytest.raises(SourceError):
        sim.start_scan()                       # ainda não aberto
    with pytest.raises(SourceError):
        sim.open("COM99")
    sim.open("SIMULADOR")
    assert sim.get_info().model == "SIMULADOR"
    assert sim.get_health().status.value == "ok"
    sim.start_scan()
    it = sim.scans()
    scans = [next(it) for _ in range(3)]
    assert all(len(s.angle_deg) == 500 for s in scans)
    sim.stop_scan()
    assert list(it) == []                       # iterador termina após stop_scan
    sim.close()
    sim.close()                                 # idempotente


def test_simulator_rate_about_10hz():
    sim = Simulator(seed=0)
    sim.open("SIMULADOR")
    sim.start_scan()
    it = sim.scans()
    t0 = time.monotonic()
    for _ in range(6):
        next(it)
    dt = time.monotonic() - t0                  # 5 intervalos de ~0,1 s
    assert 0.4 < dt < 0.7
    sim.close()


def test_pose_changes_view_and_rover_drives():
    # Sala da cena padrão: parede da frente a 3000 mm. Andando 1 m para frente, fica a 2000 mm.
    scene = Scene(segments=ROOM)
    rng = np.random.default_rng(0)
    d0 = simulate_scan(scene, rng, **CLEAN, pose=(0, 0, 0)).distance_mm
    d1 = simulate_scan(scene, rng, **CLEAN, pose=(0, 1000, 0)).distance_mm
    assert d0[0] == pytest.approx(3000) and d1[0] == pytest.approx(2000)
    # Rumo 90° (olhando para +X): a frente do sensor enxerga a parede da direita (3500 mm).
    assert simulate_scan(scene, rng, **CLEAN, pose=(0, 0, 90)).distance_mm[0] == pytest.approx(3500)

    sim = Simulator(seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim.drive(1, 1)
    sim._advance(1.0)
    assert sim._pose[:2] == pytest.approx([0, Simulator.SPEED_MM_S]) and sim._pose[2] == 0
    sim.drive(1, -1)                      # esquerda p/ frente, direita p/ trás = giro horário
    sim._advance(90 / Simulator.TURN_DEG_S)   # tempo de girar 90°
    assert sim._pose[2] == pytest.approx(90)
    sim.drive(1, 1)
    for _ in range(100):
        sim._advance(1.0)                 # para na parede (folga de 300 mm), nunca a atravessa
    assert sim._pose[0] == pytest.approx(3500 - Simulator.MARGIN_MM)


def test_rover_stops_when_commands_stop_arriving():
    sim = Simulator(seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim.drive(1, 1)
    sim._cmd_at -= Simulator.DRIVE_TIMEOUT_S + 0.1      # o último comando ficou velho
    sim._advance(1.0)
    assert sim._pose == [0.0, 0.0, 0.0]


def test_rover_commands_match_firmware_table():
    from backend.rover import sides
    assert sides("F", 255) == (1.0, 1.0) and sides("P", 255) == (0.0, 0.0)
    assert sides("E", 255) == (-1.0, 1.0)                 # esquerda: gira para a esquerda
    assert sides("G", 255) == (0.5, 1.0)                  # frente-esquerda: lado de dentro na metade
    assert sides("H", 255) == (-0.5, -1.0)
    assert sides("F", 0) == (0.0, 0.0)


def test_scan_pose_puts_points_back_on_the_fixed_room():
    # Com a pose, a rotação+translação do plot.js leva os pontos de volta às paredes fixas da sala.
    scene = Scene(segments=ROOM)
    pose = (500.0, -300.0, 37.0)
    frame = scan_to_frame(simulate_scan(scene, np.random.default_rng(0), **CLEAN, pose=pose))
    assert frame.pose == pose
    px, py, h = pose
    c, s = math.cos(math.radians(h)), math.sin(math.radians(h))
    for x, y in zip(frame.x, frame.y):
        wx, wy = px + x * c + y * s, py - x * s + y * c   # mesma fórmula de frontend/plot.js
        assert min(abs(wx + 2500), abs(wx - 3500), abs(wy + 2000), abs(wy - 3000)) < 3


def test_encode_scan_sends_pose_only_when_present():
    import json
    from backend.hub import encode_scan
    from backend.models import Frame
    assert "pose" not in json.loads(encode_scan(1, 0.0, Frame(x=[1], y=[2], q=[3])))
    msg = json.loads(encode_scan(1, 0.0, Frame(x=[1], y=[2], q=[3], pose=(10.4, -20.6, 33.33))))
    assert msg["pose"] == [10, -21, 33.3]
