"""Friendliness + Command Deck shell — Auto mode, outcomes, viewport rail."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from hybridagent.daemon import _DASHBOARD_HTML, Daemon, _find_port
from hybridagent.llm import LLMClient


def test_dashboard_embeds_friendliness_surface():
    html = _DASHBOARD_HTML
    assert "/web/friendliness.js" in html
    assert "/web/friendliness.css" in html
    assert "/web/shell.css" in html
    assert "/web/shell.js" in html
    assert 'id="healthBanner"' in html
    assert 'id="intentChip"' in html
    assert 'id="modeAuto"' in html
    assert 'id="apprBadge"' in html
    assert 'id="deckRail"' in html
    assert "rail-tabs" in html
    assert "let mode = 'auto'" in html
    assert "resolveSendMode" in html
    assert "attachOutcome" in html
    assert "PraxisFriendly" in html
    js = Path("hybridagent/web/friendliness.js").read_text(encoding="utf-8")
    assert "PraxisIntent" in js
    assert "PraxisFriendly" in js
    assert "markTour" in js
    css = Path("hybridagent/web/friendliness.css").read_text(encoding="utf-8")
    assert ".tour-hint" in css
    assert ".mission.done" in css
    shell = Path("hybridagent/web/shell.css").read_text(encoding="utf-8")
    assert ".rail-tabs" in shell
    assert "#deckRail" in shell
    sjs = Path("hybridagent/web/shell.js").read_text(encoding="utf-8")
    assert "PraxisShell" in sjs


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
        assert "PraxisFriendly" in js
        with urllib.request.urlopen(f"{url}/web/friendliness.css", timeout=10) as r:
            css = r.read().decode()
        assert "#healthBanner" in css and ".missions" in css
        assert ".tour-hint" in css
        with urllib.request.urlopen(f"{url}/web/shell.css", timeout=10) as r:
            scss = r.read().decode()
        assert ".rail-tabs" in scss
        with urllib.request.urlopen(f"{url}/web/shell.js", timeout=10) as r:
            sjs = r.read().decode()
        assert "PraxisShell" in sjs
        with urllib.request.urlopen(f"{url}/", timeout=10) as r:
            html = r.read().decode()
        assert "/web/friendliness.js" in html
        assert "/web/shell.js" in html
        assert 'id="modeAuto"' in html
        assert 'id="deckRail"' in html
        assert "attachOutcome" in html
        assert 'id="seg-chat"' not in html
        with urllib.request.urlopen(f"{url}/status", timeout=10) as r:
            st = json.loads(r.read())
        assert st.get("port") == port
    finally:
        d._stop_status_server()
