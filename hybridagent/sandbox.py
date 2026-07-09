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

Selected via ``agents.sandbox.backend`` (local|docker|auto) in config. Default is ``auto`` (Docker when available, else local). ``auto``
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

_VALID_BACKENDS = {"local", "docker", "auto", "ssh", "modal", "daytona"}
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


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def select_backend() -> str:
    """Resolve the effective backend from config, honoring availability.

    ``backend: docker`` is fail-closed: if Docker is unavailable it stays
    ``docker`` (and :func:`run` returns an error) unless the operator sets
    ``agents.sandbox.allow_local_fallback: true``. ``auto`` still prefers Docker
    when present and falls back to local otherwise.
    """
    block = _config_block()
    choice = (block.get("backend") or "auto").strip().lower()
    if choice not in _VALID_BACKENDS:
        choice = "local"
    if choice == "docker":
        if _docker_available():
            return "docker"
        if block.get("allow_local_fallback"):
            _log.warning("sandbox backend 'docker' requested but Docker is "
                         "unavailable; allow_local_fallback=true -> 'local'")
            return "local"
        _log.error("sandbox backend 'docker' requested but Docker is "
                   "unavailable; refusing local fallback (fail-closed)")
        return "docker"
    if choice == "auto":
        return "docker" if _docker_available() else "local"
    if choice == "ssh":
        if block.get("ssh_host"):
            return "ssh"
        _log.warning("sandbox backend 'ssh' requested but agents.sandbox.ssh_host "
                     "is unset; falling back to 'local'")
        return "local"
    if choice in ("modal", "daytona"):
        if _tool_available(choice):
            return choice
        _log.warning("sandbox backend '%s' requested but its CLI is not "
                     "installed; falling back to 'local'", choice)
        return "local"
    return "local"


def _run_local(command, workdir: str, timeout: float,
               env: dict | None) -> ExecResult:
    # A string command runs through the HOST shell (cmd.exe on Windows, /bin/sh
    # on POSIX) via shell=True, so local execution works cross-platform. A list
    # command is executed argv-style (no shell). Hardcoding "sh -c" broke Windows.
    use_shell = isinstance(command, str)
    try:
        proc = subprocess.run(
            command, cwd=workdir, capture_output=True, text=True,
            timeout=timeout, env={**os.environ, **(env or {})}, check=False,
            shell=use_shell)
        return ExecResult(proc.returncode == 0, proc.returncode,
                          proc.stdout, proc.stderr, "local")
    except subprocess.TimeoutExpired:
        return ExecResult(False, 124, "", "timed out", "local", "timeout")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, 1, "", str(exc), "local", "error")


def _as_posix_argv(command) -> list[str]:
    """Wrap a string command for a POSIX target (docker container / ssh host),
    where /bin/sh is always present regardless of the host OS."""
    if isinstance(command, str):
        return ["sh", "-c", command]
    return command


def _run_docker(command: list[str], workdir: str, timeout: float,
                env: dict | None, *, image: str, network: str,
                mem: str, pids: int) -> ExecResult:
    """Run inside a throwaway, locked-down container.

    Hardening: --rm (ephemeral), read-only root fs with a writable workdir mount,
    dropped Linux capabilities, no-new-privileges, network off by default,
    memory + pids caps, non-root user.
    """
    abs_workdir = os.path.abspath(workdir)
    # Run as the HOST's uid:gid (not a hardcoded 1000) so the bind-mounted /work
    # is writable regardless of the host account — hardcoding 1000:1000 broke any
    # host where the user isn't uid 1000 (root in CI, uid!=1000, rootless Docker,
    # macOS), making every sandboxed write fail. getuid is POSIX-only; fall back
    # to the documented 1000 where it's unavailable (e.g. Windows daemon host).
    try:
        uidgid = f"{os.getuid()}:{os.getgid()}"  # type: ignore[attr-defined]
    except AttributeError:
        uidgid = "1000:1000"
    docker_cmd = [
        "docker", "run", "--rm", "-i",
        "--network", network,
        "--memory", mem, "--pids-limit", str(pids),
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--read-only", "--tmpfs", "/tmp",
        "-v", f"{abs_workdir}:/work:rw", "-w", "/work",
        "--user", uidgid,
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


def _run_ssh(command: list[str], workdir: str, timeout: float,
             env: dict | None, *, host: str, key: str | None,
             remote_dir: str | None) -> ExecResult:
    """Run a command on a remote host over SSH (a 'serverless'/offloaded backend).

    The host is operator-configured and trusted; we shell-quote the remote
    command and run it in the configured remote working directory. This offloads
    heavy/risky execution off the local machine entirely.
    """
    import shlex
    remote_parts = []
    for k, v in (env or {}).items():
        remote_parts.append(f"{k}={shlex.quote(str(v))}")
    if remote_dir:
        remote_parts.append(f"cd {shlex.quote(remote_dir)} &&")
    remote_parts.append(" ".join(shlex.quote(c) for c in command))
    remote_cmd = " ".join(remote_parts)
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if key:
        ssh += ["-i", key]
    ssh += [host, remote_cmd]
    try:
        proc = subprocess.run(ssh, capture_output=True, text=True,
                              timeout=timeout, check=False)
        return ExecResult(proc.returncode == 0, proc.returncode,
                          proc.stdout, proc.stderr, "ssh")
    except subprocess.TimeoutExpired:
        return ExecResult(False, 124, "", "timed out", "ssh", "timeout")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, 1, "", str(exc), "ssh", "error")


def _run_cli_sandbox(command: list[str], timeout: float, env: dict | None,
                     *, tool: str, run_args: list[str]) -> ExecResult:
    """Run a command via a serverless-sandbox CLI (modal/daytona).

    Uses the provider CLI's run/exec subcommand from config so we don't hardcode
    a fast-moving CLI surface: ``agents.sandbox.<tool>_run`` supplies the argv
    prefix (e.g. ['modal','run','...'] or ['daytona','exec','-w','ws','--']).
    """
    full = list(run_args) + command
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **(env or {})}, check=False)
        return ExecResult(proc.returncode == 0, proc.returncode,
                          proc.stdout, proc.stderr, tool)
    except subprocess.TimeoutExpired:
        return ExecResult(False, 124, "", "timed out", tool, "timeout")
    except Exception as exc:  # noqa: BLE001
        return ExecResult(False, 1, "", str(exc), tool, "error")


def run(command, workdir: str = ".", timeout: float = 60.0,
        env: dict | None = None, backend: str | None = None) -> ExecResult:
    """Run a command under the configured (or given) isolation backend.

    ``command`` may be a list (argv, run without a shell) or a string. A string
    runs through the HOST shell for the local backend (cmd.exe/sh, cross-platform)
    and through ``/bin/sh`` for the POSIX targets (docker container / ssh host).
    """
    eff = backend or select_backend()
    block = _config_block()
    if eff == "docker":
        if not _docker_available():
            return ExecResult(
                False, 1, "",
                "Docker backend requested but Docker is unavailable "
                "(set agents.sandbox.allow_local_fallback=true to fall back)",
                "docker", "docker_unavailable")
        return _run_docker(
            _as_posix_argv(command), workdir, timeout, env,
            image=block.get("image", _DEFAULT_IMAGE),
            network=block.get("network", "none"),
            mem=str(block.get("memory", "512m")),
            pids=int(block.get("pids_limit", 256)))
    if eff == "ssh":
        return _run_ssh(_as_posix_argv(command), workdir, timeout, env,
                        host=block.get("ssh_host", ""),
                        key=block.get("ssh_key"),
                        remote_dir=block.get("remote_dir"))
    if eff in ("modal", "daytona"):
        run_args = block.get(f"{eff}_run")
        if not run_args:
            _log.warning("backend '%s' has no agents.sandbox.%s_run configured; "
                         "running locally", eff, eff)
            return _run_local(command, workdir, timeout, env)
        return _run_cli_sandbox(_as_posix_argv(command), timeout, env, tool=eff,
                                run_args=run_args)
    return _run_local(command, workdir, timeout, env)


def backend_status() -> dict:
    """Report the configured + effective backend (for `praxis doctor` / dashboard)."""
    block = _config_block()
    return {
        "configured": (block.get("backend") or "auto"),
        "effective": select_backend(),
        "docker_available": _docker_available(),
        "image": block.get("image", _DEFAULT_IMAGE),
        "network": block.get("network", "none"),
    }
