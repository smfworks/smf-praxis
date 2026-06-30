"""Messaging gateways — let Praxis reach you where you are.

Outbound delivery to messaging platforms so cron jobs, alerts, and agent results
can be pushed to Telegram / Slack / Discord / a generic webhook / ntfy. Stdlib
``urllib`` only (dependency-free core preserved); each channel is configured under
``agents.gateways.<channel>`` in praxis.json and may reference secrets via
``${ENV_VAR}``.

A delivery *target* is either:
* a bare channel name -> ``"telegram"`` (uses that channel's default config), or
* ``"<channel>:<destination>"`` -> ``"telegram:12345678"`` /
  ``"discord:https://discord.com/api/webhooks/..."`` to override the destination.

This module is intentionally **outbound-first**: it makes Praxis proactive
(notifications, scheduled briefings). Inbound (receiving messages and replying)
is a larger surface tracked separately; the config shape here is forward-
compatible with it.

Design: every send returns a :class:`DeliveryResult` and never raises into the
caller (cron/alerts must not crash on a bad webhook); failures are reported.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass

from .logging_util import get_logger

_log = get_logger("praxis.gateways")


@dataclass
class DeliveryResult:
    ok: bool
    channel: str
    detail: str = ""


def _expand_env(value: str) -> str:
    return re.sub(r"\$\{([A-Z0-9_]+)\}",
                  lambda m: os.environ.get(m.group(1), ""), value or "")


def _post_json(url: str, payload: dict, headers: dict | None = None,
               timeout: float = 15.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------- channels
def _send_telegram(cfg_block: dict, text: str, dest: str | None) -> DeliveryResult:
    token = _expand_env(cfg_block.get("bot_token", ""))
    chat_id = dest or str(cfg_block.get("chat_id", ""))
    if not token or not chat_id:
        return DeliveryResult(False, "telegram",
                              "missing bot_token or chat_id")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        status, _ = _post_json(url, {"chat_id": chat_id, "text": text[:4096]})
        return DeliveryResult(status == 200, "telegram", f"HTTP {status}")
    except Exception as exc:
        return DeliveryResult(False, "telegram", str(exc))


def _send_slack(cfg_block: dict, text: str, dest: str | None) -> DeliveryResult:
    # Incoming-webhook URL (simplest, no scopes); dest can override the URL.
    url = _expand_env(dest or cfg_block.get("webhook_url", ""))
    if not url:
        return DeliveryResult(False, "slack", "missing webhook_url")
    try:
        status, _ = _post_json(url, {"text": text})
        return DeliveryResult(status == 200, "slack", f"HTTP {status}")
    except Exception as exc:
        return DeliveryResult(False, "slack", str(exc))


def _send_discord(cfg_block: dict, text: str, dest: str | None) -> DeliveryResult:
    url = _expand_env(dest or cfg_block.get("webhook_url", ""))
    if not url:
        return DeliveryResult(False, "discord", "missing webhook_url")
    try:
        # Discord caps content at 2000 chars.
        status, _ = _post_json(url, {"content": text[:2000]})
        return DeliveryResult(status in (200, 204), "discord", f"HTTP {status}")
    except Exception as exc:
        return DeliveryResult(False, "discord", str(exc))


def _send_webhook(cfg_block: dict, text: str, dest: str | None) -> DeliveryResult:
    # Generic JSON webhook: POST {"text": ...} to a configured URL.
    url = _expand_env(dest or cfg_block.get("url", ""))
    if not url:
        return DeliveryResult(False, "webhook", "missing url")
    field = cfg_block.get("field", "text")
    try:
        status, _ = _post_json(url, {field: text})
        return DeliveryResult(200 <= status < 300, "webhook", f"HTTP {status}")
    except Exception as exc:
        return DeliveryResult(False, "webhook", str(exc))


def _send_ntfy(cfg_block: dict, text: str, dest: str | None) -> DeliveryResult:
    # ntfy.sh-style pub/sub: POST the body to <server>/<topic>.
    server = (cfg_block.get("server") or "https://ntfy.sh").rstrip("/")
    topic = dest or cfg_block.get("topic", "")
    if not topic:
        return DeliveryResult(False, "ntfy", "missing topic")
    try:
        req = urllib.request.Request(
            f"{server}/{topic}", data=text.encode("utf-8"), method="POST")
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return DeliveryResult(resp.status == 200, "ntfy", f"HTTP {resp.status}")
    except Exception as exc:
        return DeliveryResult(False, "ntfy", str(exc))


_CHANNELS = {
    "telegram": _send_telegram,
    "slack": _send_slack,
    "discord": _send_discord,
    "webhook": _send_webhook,
    "ntfy": _send_ntfy,
}


def available_channels() -> list[str]:
    return sorted(_CHANNELS)


def _gateways_config(store=None) -> dict:
    from . import config as cfg
    return cfg.load_config().get("agents", {}).get("gateways", {}) or {}


def deliver(target: str, text: str, store=None) -> DeliveryResult:
    """Send ``text`` to a delivery target.

    ``target`` is ``"<channel>"`` or ``"<channel>:<destination>"``. The channel's
    config block is read from ``agents.gateways.<channel>``; the optional
    destination overrides the channel's default (chat id / webhook url / topic).
    Never raises — returns a :class:`DeliveryResult`.
    """
    target = (target or "").strip()
    if not target or target == "local":
        return DeliveryResult(True, "local", "not delivered (local)")
    channel, _, dest = target.partition(":")
    channel = channel.strip().lower()
    dest = dest.strip() or None
    sender = _CHANNELS.get(channel)
    if sender is None:
        return DeliveryResult(False, channel,
                              f"unknown channel '{channel}' "
                              f"(have: {', '.join(available_channels())})")
    cfg_block = _gateways_config(store).get(channel, {}) or {}
    result = sender(cfg_block, text, dest)
    if not result.ok:
        _log.warning("gateway delivery failed [%s]: %s", channel, result.detail)
    return result


def configured_targets(store=None) -> list[str]:
    """Channels that have a config block (best-effort 'what's wired' view)."""
    return sorted(_gateways_config(store).keys())
