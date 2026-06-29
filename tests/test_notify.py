"""Tests for notify-on-block/done: gating, event filter, dispatch, status mapping."""

from hybridagent import config as cfg
from hybridagent import notify


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_disabled_by_default_is_noop(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert notify.Notifier().notify("done", "t", "d") is False


def test_status_event_mapping():
    assert notify.status_event("completed") == "done"
    assert notify.status_event("waiting_approval") == "blocked"
    assert notify.status_event("failed") == "failed"
    assert notify.status_event("running") is None


def test_event_filter_respected(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    n = notify.Notifier({"url": "http://x", "events": ["blocked"]})
    assert n.enabled_for("blocked") and not n.enabled_for("done")


def test_webhook_dispatch_posts_payload(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    seen = {}

    class _Resp:
        def close(self):
            pass

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_open)
    assert notify.Notifier({"url": "http://hook"}).notify("done", "Title", "Detail")
    assert seen["url"] == "http://hook" and "done" in seen["body"] and "Detail" in seen["body"]
