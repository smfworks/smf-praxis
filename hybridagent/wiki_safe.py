"""URL scheme/host allowlist for KB source ingestion.

Regulated deployments cannot accept arbitrary URIs from the operator (or worse,
from an LLM-generated suggestion). This module gates :class:`~hybridagent.wiki.KBSourceManager`
fetches against an explicit allowlist:

* only ``http`` / ``https`` schemes are accepted (no ``file://``, ``ftp://``,
  ``data:``, etc.);
* private, loopback, link-local, and multicast hosts are rejected unless the
  operator explicitly opts in via ``PRAXIS_KB_ALLOW_PRIVATE=1``;
* responses are read with a hard size cap (default 5 MiB) and timeout (30 s) so
  a hostile or runaway source can't exhaust memory.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import urllib.parse
import urllib.request

MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0


class UnsafeSourceError(RuntimeError):
    """Raised when a KB source URI fails the safety allowlist."""


def _allow_private() -> bool:
    return os.environ.get("PRAXIS_KB_ALLOW_PRIVATE", "0") == "1"


def _host_is_private(host: str) -> bool:
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # If DNS can't resolve, treat as unsafe; we'd rather refuse than blindly
        # hand the URL to urllib.
        return True
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            return True
    return False


def validate_uri(uri: str) -> str:
    parsed = urllib.parse.urlparse(uri)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise UnsafeSourceError(
            f"refusing scheme {scheme!r}: only http/https URIs may be ingested "
            f"(file://, data:, ftp:// are blocked)."
        )
    host = parsed.hostname or ""
    if not host:
        raise UnsafeSourceError("refusing URI with empty host")
    if _host_is_private(host) and not _allow_private():
        raise UnsafeSourceError(
            f"refusing private/loopback/internal host {host!r}; set "
            f"PRAXIS_KB_ALLOW_PRIVATE=1 to opt in (e.g. for an intranet wiki)."
        )
    return uri


def fetch_url(uri: str, timeout: float = DEFAULT_TIMEOUT,
              max_bytes: int = MAX_BYTES,
              user_agent: str = "praxis-kb/1.0") -> str:
    """Fetch a validated URL with size + timeout guards."""
    validate_uri(uri)
    req = urllib.request.Request(uri, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise UnsafeSourceError(
            f"source exceeds maximum size of {max_bytes} bytes"
        )
    return raw.decode(errors="replace")
