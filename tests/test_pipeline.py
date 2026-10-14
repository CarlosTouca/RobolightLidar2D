import numpy as np
import pytest

from backend.models import Scan
from backend.pipeline import scan_to_frame


def mk(angles, dists, quals):
    return Scan(np.array(angles, float), np.array(dists, float), np.array(quals, int))


def test_cardinal_directions():
    # 0° = frente (+Y); 90° = direita (+X); 180° = trás (-Y); 270° = esquerda (-X)
    f = scan_to_frame(mk([0, 90, 180, 270], [1000, 2000, 3000, 4000], [10] * 4))
    assert f.x == [0, 2000, 0, -4000]
    assert f.y == [1000, 0, -3000, 0]


def test_clockwise_diagonal():
    f = scan_to_frame(mk([45], [1000], [10]))
    assert f.x == [707] and f.y == [707]


def test_discards_zero_quality_and_zero_distance():
    f = scan_to_frame(mk([0, 10, 20, 30], [1000, 1000, 0, 0], [5, 0, 5, 0]))
    assert len(f.x) == len(f.y) == len(f.q) == 1
    assert f.q == [5]


def test_output_is_plain_int_lists():
    f = scan_to_frame(mk([10.4, 200.7], [1234.5, 987.6], [3, 63]))
    for arr in (f.x, f.y, f.q):
        assert type(arr) is list
        assert all(type(v) is int for v in arr)


def test_empty_and_all_invalid():
    assert scan_to_frame(mk([], [], [])).x == []
    assert scan_to_frame(mk([1, 2], [0, 0], [0, 0])).x == []


def test_mismatched_lengths_rejected():
    with pytest.raises(ValueError):
        mk([1, 2], [1000], [5])
