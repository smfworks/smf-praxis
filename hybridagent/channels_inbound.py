"""Inbound messaging — Telegram long-poll / webhook and Slack Events API.

Maps external chat messages into governed Praxis turns and can push approval
deep-links back to the channel. Config under ``agents.gateways.telegram`` /
``agents.gateways.slack`` (reuses outbound keys).

Conversation threads are durable when a :class:`~hybridagent.persistence.Store`
is supplied (or resolved via :func:`set_thread_store`); otherwise they fall back
to process memory so unit tests stay dependency-free.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from . import config as cfg
from .logging_util import get_logger

if TYPE_CHECKING:
    from .persistence import Store

_log = get_logger("praxis.channels")

# Process-local fallback when no Store is bound (tests / short-lived CLI).
_THREADS: dict[str, list[dict]] = {}
_THREAD_STORE: "Store | None" = None
_MAX_THREAD_MESSAGES = 40


def set_thread_store(store: "Store | None") -> None:
    """Bind the durable store used for channel thread continuity."""
    global _THREAD_STORE
    _THREAD_STORE = store


def thread_store() -> "Store | None":
    return _THREAD_STORE


def _expand_env(value: str) -> str:
    return re.sub(r"\$\{([A-Z0-9_]+)\}",
                  lambda m: os.environ.get(m.group(1), ""), value or "")


def _gateway(channel: str) -> dict:
    return ((cfg.load_config().get("agents") or {})
            .get("gateways") or {}).get(channel) or {}


@dataclass
class InboundMessage:
    channel: str
    text: str
    sender: str
    chat_id: str
    raw: dict


def telegram_enabled() -> bool:
    b = _gateway("telegram")
    return bool(b.get("enabled") and _expand_env(b.get("bot_token", "")))


def slack_enabled() -> bool:
    b = _gateway("slack")
    return bool(b.get("enabled"))


def approval_deep_link(base_url: str, approval_id: str) -> str:
    base = (base_url or "").rstrip("/")
    return f"{base}/?approve={urllib.parse.quote(approval_id)}"


def telegram_send(text: str, chat_id: str | None = None) -> dict:
    b = _gateway("telegram")
    token = _expand_env(b.get("bot_token", ""))
    cid = chat_id or str(b.get("chat_id", ""))
    if not token or not cid:
        return {"ok": False, "error": "missing bot_token or chat_id"}
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps({"chat_id": cid, "text": text[:4096],
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def telegram_poll_updates(offset: int = 0, timeout: int = 0) -> list[dict]:
    b = _gateway("telegram")
    token = _expand_env(b.get("bot_token", ""))
    if not token:
        return []
    q = urllib.parse.urlencode({"offset": offset, "timeout": timeout,
                                "allowed_updates": json.dumps(["message"])})
    url = f"https://api.telegram.org/bot{token}/getUpdates?{q}"
    try:
        with urllib.request.urlopen(url, timeout=max(25, timeout + 5)) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001
        _log.warning("telegram poll failed: %s", exc)
        return []
    if not body.get("ok"):
        return []
    return list(body.get("result") or [])


def parse_telegram_update(update: dict) -> InboundMessage | None:
    msg = (update or {}).get("message") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return None
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    from_u = msg.get("from") or {}
    sender = str(from_u.get("username") or from_u.get("id") or "telegram")
    b = _gateway("telegram")
    allow = str(b.get("chat_id") or "").strip()
    if allow:
        allowed = {x.strip() for x in allow.split(",") if x.strip()}
        if chat_id not in allowed and allow not in (chat_id,):
            _log.info("telegram message from non-allowlisted chat %s", chat_id)
            return None
    return InboundMessage("telegram", text, sender, chat_id, update)


def parse_slack_event(payload: dict) -> InboundMessage | None | dict:
    """Return challenge dict, InboundMessage, or None."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    event = payload.get("event") or {}
    if event.get("type") != "message" or event.get("bot_id") or event.get("subtype"):
        return None
    text = (event.get("text") or "").strip()
    if not text:
        return None
    return InboundMessage(
        "slack", text,
        str(event.get("user") or "slack"),
        str(event.get("channel") or ""),
        payload,
    )


def verify_slack_signature(signing_secret: str, timestamp: str,
                           body: bytes, signature: str) -> bool:
    if not signing_secret:
        return True
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > 60 * 5:
        return False
    basestring = f"v0:{timestamp}:{body.decode('utf-8', errors='replace')}"
    digest = "v0=" + hmac.new(
        signing_secret.encode(), basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, signature or "")


def slack_reply(text: str, channel: str) -> dict:
    b = _gateway("slack")
    token = _expand_env(b.get("bot_token", ""))
    if token and channel:
        url = "https://slack.com/api/chat.postMessage"
        data = json.dumps({"channel": channel, "text": text[:3000]}).encode()
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode() or "{}")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
    from .gateways import deliver
    res = deliver("slack", text)
    return {"ok": res.ok, "detail": res.detail}


def thread_key(channel: str, chat_id: str) -> str:
    return f"{channel}:{chat_id}"


def get_thread(channel: str, chat_id: str,
               store: "Store | None" = None) -> list[dict]:
    key = thread_key(channel, chat_id)
    st = store if store is not None else _THREAD_STORE
    if st is not None:
        try:
            return list(st.get_channel_thread(key))
        except Exception as exc:  # noqa: BLE001
            _log.warning("channel thread load failed: %s", exc)
    return list(_THREADS.get(key, []))


def append_thread(channel: str, chat_id: str, role: str, content: str,
                  store: "Store | None" = None) -> list[dict]:
    key = thread_key(channel, chat_id)
    st = store if store is not None else _THREAD_STORE
    hist = get_thread(channel, chat_id, store=st)
    hist.append({"role": role, "content": content})
    if len(hist) > _MAX_THREAD_MESSAGES:
        hist = hist[-_MAX_THREAD_MESSAGES:]
    if st is not None:
        try:
            st.set_channel_thread(key, channel, chat_id, hist,
                                  max_messages=_MAX_THREAD_MESSAGES)
        except Exception as exc:  # noqa: BLE001
            _log.warning("channel thread save failed: %s", exc)
            _THREADS[key] = hist
    else:
        _THREADS[key] = hist
    return list(hist)


def handle_inbound(
    msg: InboundMessage,
    chat_fn: Callable[[list[dict]], str],
    *,
    base_url: str = "",
    approvals: list[dict] | None = None,
    store: "Store | None" = None,
) -> str:
    """Run a governed chat turn and reply; append approval links when held."""
    st = store if store is not None else _THREAD_STORE
    hist = append_thread(msg.channel, msg.chat_id, "user", msg.text, store=st)
    m = re.match(r"(?i)^(?:/)?approve(?:\s+|:)(\S+)", msg.text.strip())
    if m:
        return f"APPROVE_CMD:{m.group(1)}"
    m = re.match(r"(?i)^(?:/)?deny(?:\s+|:)(\S+)", msg.text.strip())
    if m:
        return f"DENY_CMD:{m.group(1)}"
    try:
        reply = chat_fn(hist)
    except Exception as exc:  # noqa: BLE001
        reply = f"Sorry — I hit an error: {exc}"
    append_thread(msg.channel, msg.chat_id, "assistant", reply, store=st)
    if approvals:
        links = []
        for a in approvals[:5]:
            aid = a.get("approval_id") or ""
            if not aid:
                continue
            link = approval_deep_link(base_url, aid) if base_url else aid
            links.append(f"• {a.get('tool', 'action')}: approve {aid}\n  {link}")
        if links:
            reply += "\n\n⏸ Held for your approval:\n" + "\n".join(links)
            reply += "\n\nReply `approve <id>` or `deny <id>`."
    return reply


# ------------------------------------------------------------------ config UI
def configure_telegram(*, bot_token: str = "", chat_id: str = "",
                       enabled: bool = True,
                       use_env_ref: bool = False) -> dict:
    """Enable Telegram gateway from Settings / API.

    When ``use_env_ref`` is True the token is stored as ``${TELEGRAM_BOT_TOKEN}``
    rather than the raw secret (recommended). Raw tokens are accepted for
    one-click paste and written only into local config (gitignored home).
    """
    conf = cfg.load_config()
    agents = conf.setdefault("agents", {})
    gateways = agents.setdefault("gateways", {})
    tg = dict(gateways.get("telegram") or {})
    token = (bot_token or "").strip()
    if use_env_ref or token.upper() in ("${TELEGRAM_BOT_TOKEN}", "ENV", "ENV_REF"):
        tg["bot_token"] = "${TELEGRAM_BOT_TOKEN}"
    elif token:
        tg["bot_token"] = token
    cid = (chat_id or "").strip()
    if cid:
        tg["chat_id"] = cid
    tg["enabled"] = bool(enabled)
    gateways["telegram"] = tg
    cfg.save_config(conf)
    return telegram_status()


def disable_telegram() -> dict:
    conf = cfg.load_config()
    agents = conf.setdefault("agents", {})
    gateways = agents.setdefault("gateways", {})
    tg = dict(gateways.get("telegram") or {})
    tg["enabled"] = False
    gateways["telegram"] = tg
    cfg.save_config(conf)
    return telegram_status()


def telegram_status() -> dict:
    b = _gateway("telegram")
    token = _expand_env(b.get("bot_token", ""))
    raw = str(b.get("bot_token") or "")
    return {
        "enabled": bool(b.get("enabled")),
        "configured": bool(token or raw),
        "has_token": bool(token),
        "token_is_env_ref": raw.strip().startswith("${"),
        "chat_id": str(b.get("chat_id") or ""),
        "polling": bool(b.get("enabled") and token),
        "hint": (
            "Telegram is live — message your bot; reply with approve <id> for holds."
            if (b.get("enabled") and token) else
            "Paste a bot token from @BotFather and your chat id, then Enable."
        ),
    }


def telegram_get_me() -> dict:
    """Probe the bot token (getMe) for the Settings UI."""
    b = _gateway("telegram")
    token = _expand_env(b.get("bot_token", ""))
    if not token:
        return {"ok": False, "error": "no bot token configured"}
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body = json.loads(resp.read().decode() or "{}")
        if body.get("ok"):
            res = body.get("result") or {}
            return {
                "ok": True,
                "username": res.get("username"),
                "first_name": res.get("first_name"),
                "id": res.get("id"),
            }
        return {"ok": False, "error": body.get("description") or "getMe failed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
