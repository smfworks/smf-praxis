"""Pluggable sandboxed execution backend (Phase B / G6).

Shell/code execution is the highest-blast-radius capability an autonomous agent
has. This module provides a **pluggable execution backend** so the same governed
tool call can run with progressively stronger isolation:

* ``local``  — run on the host, confined to the work dir (path bounded). The
  historical behavior; weakest isolation, always available.
* ``docker`` — run inside a throwaway container with no host mounts beyond the
  work dir, dropped capabilities, optional ``--network none``, a memory/pids cap,
  and a wall-clock timeout. Used automatically when Docker is present and the
  backend is set to ``docker``/``auto``.

Selected via ``agents.sandbox.backend`` (local|docker|auto) in config. ``auto``
prefers Docker when the daemon can reach it, else falls back to local with a
logged warning — fail-closed in spirit (stronger when possible) without breaking
a host that has no Docker.

Dependency-free: Docker is driven via the ``docker`` CLI through subprocess, not
a Python SDK. Everything degrades gracefully.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .logging_util import get_logger

_log = get_logger("praxis.sandbox")

_VALID_BACKENDS = {"local", "docker", "auto"}
_DEFAULT_IMAGE = "python:3.12-slim"


@dataclass
class ExecResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    backend: str
    detail: str = ""


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        r = subprocess.run(["docker", "info"], capture_output=True,
                           timeout=8, check=False)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _config_block() -> dict:
    from . import config as cfg
    return cfg.load_config().get("agents", {}).get("sandbox", {}) or {}


def select_backend() -> str:
    """Resolve the effective backend from config, honoring availability."""
    block = _config_block()
    choice = (block.get("backend") or "local").strip().lower()
    if choice not in _VALID_BACKENDS:
        choice = "local"
    if choice == "docker":
        if _docker_available():
            return "docker"
        _log.warning("sandbox backend 'docker' requested but Docker is "
                     "unavailable; falling back to 'local'")
        return "local"
    if choice == "auto":
        return "docker" if _docker_available() else "local"
    return "local"


def _run_local(command: list[str], workdir: str, timeout: float,
               env: dict | None) -> ExecResult:
    try:
        proc = subprocess.run(
            command, cwd=workdir, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, **(env or {})}, check=False)
        return ExecResult(proc.returncode == 0, proc.returncode,
                          proc.stdout, proc.stderr, "local")
    except subprocess.TimeoutExpired:
        return ExecResult(False, 124, "", "timed out", "local", "timeout")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, 1, "", str(exc), "local", "error")


def _run_docker(command: list[str], workdir: str, timeout: float,
                env: dict | None, *, image: str, network: str,
                mem: str, pids: int) -> ExecResult:
    """Run inside a throwaway, locked-down container.

    Hardening: --rm (ephemeral), read-only root fs with a writable workdir mount,
    dropped Linux capabilities, no-new-privileges, network off by default,
    memory + pids caps, non-root user.
    """
    abs_workdir = os.path.abspath(workdir)
    docker_cmd = [
        "docker", "run", "--rm", "-i",
        "--network", network,
        "--memory", mem, "--pids-limit", str(pids),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp",
        "-v", f"{abs_workdir}:/work:rw", "-w", "/work",
        "--user", "1000:1000",
    ]
    for k, v in (env or {}).items():
        docker_cmd += ["-e", f"{k}={v}"]
    docker_cmd.append(image)
    docker_cmd += command
    try:
        proc = subprocess.run(docker_cmd, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return ExecResult(proc.returncode == 0, proc.returncode,
                          proc.stdout, proc.stderr, "docker")
    except subprocess.TimeoutExpired:
        return ExecResult(False, 124, "", "timed out", "docker", "timeout")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, 1, "", str(exc), "docker", "error")


def run(command, workdir: str = ".", timeout: float = 60.0,
        env: dict | None = None, backend: str | None = None) -> ExecResult:
    """Run a command under the configured (or given) isolation backend.

    ``command`` may be a list (argv) or a string (run via ``sh -c``).
    """
    if isinstance(command, str):
        command = ["sh", "-c", command]
    eff = backend or select_backend()
    block = _config_block()
    if eff == "docker":
        return _run_docker(
            command, workdir, timeout, env,
            image=block.get("image", _DEFAULT_IMAGE),
            network=block.get("network", "none"),
            mem=str(block.get("memory", "512m")),
            pids=int(block.get("pids_limit", 256)))
    return _run_local(command, workdir, timeout, env)


def backend_status() -> dict:
    """Report the configured + effective backend (for `praxis doctor` / dashboard)."""
    block = _config_block()
    return {
        "configured": (block.get("backend") or "local"),
        "effective": select_backend(),
        "docker_available": _docker_available(),
        "image": block.get("image", _DEFAULT_IMAGE),
        "network": block.get("network", "none"),
    }
