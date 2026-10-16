"""Guardas estáticas do frontend (a verificação visual é manual; ver README)."""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app

FRONT = Path(__file__).resolve().parent.parent / "frontend"
JS = {p.name: p.read_text(encoding="utf-8") for p in FRONT.glob("*.js")}
HTML = (FRONT / "index.html").read_text(encoding="utf-8")


def test_served_by_backend_including_local_plotly():
    with TestClient(create_app()) as c:
        r = c.get("/")
        assert r.status_code == 200 and "Lidar 2D" in r.text
        for path in ("/app.js", "/plot.js", "/style.css", "/vendor/plotly-gl2d.min.js"):
            assert c.get(path).status_code == 200, path
        assert c.get("/app.js").headers["cache-control"] == "no-cache"   # nunca JS velho em cache


def test_no_cdn_or_external_resources():
    assert not re.findall(r'(?:src|href)="https?://', HTML)
    for name, src in JS.items():
        assert "cdn." not in src and "import(" not in src, name
    assert "plotly.com" not in HTML


def test_uses_react_never_newplot_and_constant_uirevision():
    plot = JS["plot.js"]
    assert "Plotly.react(" in plot
    assert all("newPlot" not in src for src in JS.values())
    # uirevision só depende de `revision`, que só é incrementada em setRange() (ação explícita)
    assert re.findall(r"uirevision:\s*([^,\n]+)", plot) == ["`lidar-${revision}`"]
    assert plot.count("revision += 1") == 1
    m = re.search(r"function setRange\(m\) \{(.*?)\n  \}", plot, re.S)
    assert m and "revision += 1" in m.group(1)


def test_scattergl_rings_and_scaleanchor():
    plot = JS["plot.js"]
    assert "'scattergl'" in plot
    assert "scaleanchor: 'x'" in plot and "scaleratio: 1" in plot
    assert "type: 'circle'" in plot


def test_ack_is_sent_in_finally_block():
    app = JS["app.js"]
    m = re.search(r"finally\s*\{(.*?)\n  \}", app, re.S)
    assert m and "ack(sock, msg.seq)" in m.group(1)
    assert "ws.send" not in app.replace("sock.send(String(seq))", "")   # único envio é o ack


def test_panel_has_required_controls():
    for id_ in ("port", "btn-refresh", "btn-connect", "btn-disconnect", "btn-start", "btn-stop",
                "st-state", "st-model", "st-firmware", "st-serial", "st-health", "st-errors", "port-desc", "error",
                "range", "btn-range", "color-q", "fps", "points", "pad-info", "view-info"):
        assert f'id="{id_}"' in HTML, id_
    assert 'role="alert"' in HTML                 # erros visíveis e anunciados, não só no console
    panel = JS["panel.js"]
    for call in ("api.ports()", "api.connect(", "api.disconnect()", "api.start()", "api.stop()"):
        assert call in panel, call
