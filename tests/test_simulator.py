import math
import time

import numpy as np
import pytest

from backend.pipeline import scan_to_frame
from backend.sources.base import SourceError
from backend.sources.simulator import (
    Scene, Simulator, default_scene, house_scene, push_out, raycast, rect_segments, simulate_scan,
    u_shape, wall,
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

    sim = Simulator(scene=Scene(segments=ROOM), seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim.steer(0, 1, 1.0)                                   # "cima" na tela, o rover já aponta para cima
    sim._advance(1.0)
    assert sim._pose[:2] == pytest.approx([0, Simulator.SPEED_MM_S]) and sim._pose[2] == 0
    def run(seconds: float, step: float = 0.05):
        for _ in range(round(seconds / step)):
            sim.steer(*_dir, 1.0)                                   # o comando se renova, como o heartbeat do navegador
            sim._advance(step)

    _dir = (1, 0)                                          # "direita": o rover gira até 90° (horário), sem passar do alvo
    headings = []
    for _ in range(30):
        sim.steer(*_dir, 1.0)
        sim._advance(0.05)
        headings.append(sim._pose[2])
    assert headings == sorted(headings) and headings[-1] <= 90 + 1e-9 and headings[-1] == pytest.approx(90, abs=1.0)
    assert max(b - a for a, b in zip(headings, headings[1:])) <= Simulator.TURN_DEG_S * 0.05 + 1e-9   # nunca mais rápido que o máximo
    _dir = (-1, 0)                                         # "esquerda": 180° de erro, vai pelo caminho mais curto
    run(2.0)
    assert sim._pose[2] == pytest.approx(270, abs=1.0)
    sim.steer(1, 0, 1.0)
    for _ in range(100):
        sim.steer(1, 0, 1.0)
        sim._advance(1.0)                                  # para na parede (folga de 300 mm), nunca a atravessa
    assert sim._pose[0] == pytest.approx(3500 - Simulator.ROVER_RADIUS_MM)


def test_rover_stops_when_commands_stop_arriving():
    sim = Simulator(seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim.steer(0, 1, 1.0)
    sim._cmd_at -= Simulator.DRIVE_TIMEOUT_S + 0.1      # o último comando ficou velho
    sim._advance(1.0)
    assert sim._pose == [0.0, 0.0, 0.0]


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


def test_wall_with_door_gaps():
    segs = wall((0, 100), (1000, 100), ((200, 300), (600, 700)))          # horizontal, duas portas
    assert segs == [((0, 100), (200, 100)), ((300, 100), (600, 100)), ((700, 100), (1000, 100))]
    assert wall((50, 0), (50, 500), ((100, 200),)) == [((50, 0), (50, 100)), ((50, 200), (50, 500))]   # vertical


def test_house_is_closed_and_start_is_free():
    scene = house_scene()
    d = raycast(scene, np.arange(0.0, 360.0, 1.0))
    assert np.all(np.isfinite(d))                                          # sem buracos: todo raio bate em algo
    assert push_out(scene, 0, 0, Simulator.ROVER_RADIUS_MM) == (0, 0)      # o rover começa em espaço livre


def test_rover_is_blocked_by_house_walls_but_goes_through_doors():
    sim = Simulator(seed=0, **CLEAN)
    sim.open("SIMULADOR")
    for _ in range(60):
        sim.steer(0, 1, 1.0)                    # de (0, 0) para cima: bate na parede do corredor (y = 1000)
        sim._advance(0.1)
    assert sim._pose[1] == pytest.approx(1000 - Simulator.ROVER_RADIUS_MM)
    sim._pose = [1500.0, 0.0, 0.0]              # em x = 1500 há uma abertura de 1,4 m na parede
    for _ in range(60):
        sim.steer(0, 1, 1.0)
        sim._advance(0.1)
    assert sim._pose[1] > 2400                  # atravessou o corredor e a porta do quarto 2


def test_rover_does_not_tunnel_through_a_wall_after_a_long_pause():
    sim = Simulator(scene=Scene(segments=ROOM), seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim._pose = [0.0, 2000.0, 0.0]
    sim.steer(0, 1, 1.0)
    sim._advance(1.5)                           # um passo enorme: sem sub-passos atravessaria a parede em y = 3000
    assert sim._pose[1] == pytest.approx(3000 - Simulator.ROVER_RADIUS_MM)


def test_pose_is_integrated_by_wall_clock_from_any_thread():
    sim = Simulator(scene=Scene(segments=ROOM), seed=0, **CLEAN)
    sim.open("SIMULADOR")
    sim.start_scan()
    sim.steer(0, 1, 1.0)
    time.sleep(0.3)
    p1 = sim.pose()                                        # o ticker de pose (60 Hz) integra sem esperar uma varredura
    time.sleep(0.2)
    p2 = sim.pose()
    assert p2[1] > p1[1] > 0 and p2[1] - p1[1] == pytest.approx(0.2 * Simulator.SPEED_MM_S, rel=0.35)
    sim.close()
