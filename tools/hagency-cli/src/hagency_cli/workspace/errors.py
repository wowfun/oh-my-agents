from __future__ import annotations

import subprocess
from typing import Never


class WorkspaceError(RuntimeError):
    """A workspace operation could not be completed."""


class GitCommandError(WorkspaceError):
    """Git could not start or exceeded its execution deadline."""


def fail(message: str) -> Never:
    raise WorkspaceError(message)


def format_called_process_error(error: subprocess.CalledProcessError) -> str:
    cmd = error.cmd
    if isinstance(cmd, list | tuple):
        rendered_cmd = " ".join(str(part) for part in cmd)
    else:
        rendered_cmd = str(cmd)
    details = (error.stderr or error.output or "").strip()
    if details:
        return f"command failed with exit {error.returncode}: {rendered_cmd}: {details}"
    return f"command failed with exit {error.returncode}: {rendered_cmd}"


class SkillReferenceError(WorkspaceError):
    def __init__(self, reference: str, references: tuple[str, ...]):
        self.reference = reference
        self.references = references
        self.message = f"skill name {reference!r} is ambiguous. Choose one:"
        super().__init__(self.message + "\n" + "\n".join(references))


class SkillNameConflictError(WorkspaceError):
    """More than one source directory would use the same installation name."""


class SkillSymlinkError(WorkspaceError):
    def __init__(self, message: str, *, windows: bool):
        self.windows = windows
        super().__init__(message)


class SourceNotReadyError(WorkspaceError):
    def __init__(self, message: str, sources: tuple[str, ...]):
        self.sources = sources
        super().__init__(message)


class SourceBatchError(WorkspaceError):
    def __init__(self, failed: tuple[str, ...], reanchor: tuple[str, ...]):
        self.failed = failed
        self.reanchor = reanchor
        super().__init__(f"source sync failed for: {', '.join(failed)}")
