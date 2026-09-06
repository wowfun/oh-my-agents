from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from hagency_cli.workspace.errors import GitCommandError
from hagency_cli.workspace.events import Progress, emit_event

GIT_COMMAND_TIMEOUT = 300.0


def git_process(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    check: bool = False,
    timeout: float = GIT_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run a bounded Git command while keeping subprocess output off the terminal."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, check=check, text=True, capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise GitCommandError(
            f"command timed out after {timeout:g}s: {shlex.join(cmd)}"
        ) from exc
    except OSError as exc:
        raise GitCommandError(f"could not run {shlex.join(cmd)}: {exc}") from exc


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    progress: Progress | None = None,
    timeout: float = GIT_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[str] | None:
    if cwd:
        emit_event(progress, f"+ cwd: {cwd}")
    emit_event(progress, "+ cmd: " + " ".join(shlex.quote(part) for part in cmd))
    if dry_run:
        return None
    result = git_process(cmd, cwd=cwd, check=True, timeout=timeout)
    if result.stdout:
        emit_event(progress, result.stdout.rstrip())
    if result.stderr:
        emit_event(progress, result.stderr.rstrip(), error=True)
    return result


def git_ok(cmd: list[str], *, cwd: Path) -> bool:
    return git_process(cmd, cwd=cwd).returncode == 0
