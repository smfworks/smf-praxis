"""Notify-on-block/done — Praxis reports like a teammate instead of going quiet.

A long-running task can stall a human's awareness even when the agent itself never
stalls. This seam pushes a short message to an operator-chosen sink when a task
crosses a notable status: it **completes**, **fails**, or **blocks** on a held
approval. It is best-effort and dependency-free: if nothing is configured (the
default) it is a silent no-op, so offline/CI behavior is unchanged.

Config (``agents.notify`` in praxis.json), all optional:

    {"agents": {"notify": {
        "events": ["done", "blocked", "failed"],   # which to send (default all)
        "url": "https://hooks.example.com/praxis",  # POST {event,title,detail}
        "command": "notify-send"                     # argv[1]=title, argv[2]=detail
    }}}

Events: ``done`` (completed), ``blocked`` (waiting_approval), ``failed``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.notify")
EVENTS = ("done", "blocked", "failed")
_STATUS_EVENT = {"completed": "done", "waiting_approval": "blocked", "failed": "failed"}


def status_event(status: str) -> str | None:
    """Map a task status to a notify event, or None if not notable."""
    return _STATUS_EVENT.get(status)


class Notifier:
    def __init__(self, conf: dict | None = None) -> None:
        self.conf = conf if conf is not None else cfg.get_notify_config()

    def enabled_for(self, event: str) -> bool:
        if not (self.conf.get("url") or self.conf.get("command")):
            return False
        return event in (self.conf.get("events") or list(EVENTS))

    def notify(self, event: str, title: str, detail: str = "") -> bool:
        """Send one notification. Returns True if dispatched; never raises."""
        if not self.enabled_for(event):
            return False
        sent = False
        url = self.conf.get("url")
        if url:
            try:
                body = json.dumps({"event": event, "title": title, "detail": detail})
                req = urllib.request.Request(
                    url, data=body.encode(), headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=5).close()  # noqa: S310
                sent = True
            except Exception as exc:
                _log.warning("notify webhook failed: %s", exc)
        cmd = self.conf.get("command")
        if cmd:
            exe = shutil.which(cmd) or cmd
            try:
                subprocess.run([exe, title, detail], timeout=5, check=False)
                sent = True
            except Exception as exc:
                _log.warning("notify command failed: %s", exc)
        return sent


def notify_task(event: str, task_id: str, detail: str = "") -> bool:
    title = f"Praxis task {task_id}: {event}"
    return Notifier().notify(event, title, detail)
