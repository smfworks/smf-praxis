"""Friendliness Sprint A — dashboard assets + Auto mode shell hooks."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from hybridagent.daemon import Daemon, _DASHBOARD_HTML, _find_port
from hybridagent.llm import LLMClient


def test_dashboard_embeds_friendliness_surface():
    html = _DASHBOARD_HTML
    assert "/web/friendliness.js" in html
    assert "/web/friendliness.css" in html
    assert 'id="healthBanner"' in html
    assert 'id="intentChip"' in html
    assert 'id="modeAuto"' in html
    assert 'id="apprBadge"' in html
    assert "let mode = 'auto'" in html
    assert "resolveSendMode" in html
    js = Path("hybridagent/web/friendliness.js").read_text(encoding="utf-8")
    assert "PraxisIntent" in js
    assert "missions" in js


def test_friendliness_assets_served(tmp_path, monkeypatch):
    from hybridagent import config as cfg

    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    port = _find_port("127.0.0.1", 30000, 30100)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{url}/web/friendliness.js", timeout=10) as r:
            js = r.read().decode()
            ctype = r.headers.get("Content-Type", "")
        assert "PraxisIntent" in js and "javascript" in ctype
        with urllib.request.urlopen(f"{url}/web/friendliness.css", timeout=10) as r:
            css = r.read().decode()
        assert "#healthBanner" in css and ".missions" in css
        with urllib.request.urlopen(f"{url}/", timeout=10) as r:
            html = r.read().decode()
        assert "/web/friendliness.js" in html
        assert 'id="modeAuto"' in html
        # Legacy segmented tabs should not be the primary control.
        assert 'id="seg-chat"' not in html
        with urllib.request.urlopen(f"{url}/status", timeout=10) as r:
            st = json.loads(r.read())
        # Status server alone (no tick loop) still reports the bound port.
        assert st.get("port") == port
    finally:
        d._stop_status_server()
