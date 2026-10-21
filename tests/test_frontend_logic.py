"""Lógica pura do frontend (sem navegador), executada com o Node. Pulado se o Node não estiver instalado."""
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
JS = Path(__file__).parent / "js"


@pytest.mark.skipif(NODE is None, reason="Node não encontrado")
@pytest.mark.parametrize("script", sorted(JS.glob("*_test.mjs")), ids=lambda p: p.name)
def test_frontend_logic(script):
    r = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
