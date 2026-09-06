from __future__ import annotations

import os
import stat
import subprocess
from itertools import pairwise
from pathlib import Path, PurePosixPath

from pathspec import GitIgnoreSpec

from hagency_cli.files.sync.models import (
    GIT_COMMAND_TIMEOUT_SECONDS,
    ActionKind,
    EntryKind,
    FileEntry,
    FileSyncConfigError,
    FileSyncError,
    Snapshot,
    SyncAction,
)


class IgnoreMatcher:
    def __init__(
        self,
        patterns: tuple[str, ...],
        protected_paths: tuple[PurePosixPath, ...] = (),
        *,
        protect_git: bool = False,
    ) -> None:
        self.patterns = patterns
        self.protected_paths = frozenset(protected_paths)
        self._protect_git = protect_git
        self._protected_casefold = {
            protected.as_posix().casefold() for protected in protected_paths
        }
        self._spec = GitIgnoreSpec.from_lines(patterns)

    def matches(self, path: PurePosixPath, *, directory: bool) -> bool:
        # These metadata paths stay protected even on case-insensitive targets
        # and cannot be re-included by user or bundle negation rules.
        parts = tuple(part.casefold() for part in path.parts)
        if self._protect_git and ".git" in parts:
            return True
        if any(
            parent == ".vscode" and child == "sftp.json"
            for parent, child in pairwise(parts)
        ):
            return True
        if path.as_posix().casefold() in self._protected_casefold:
            return True
        value = path.as_posix()
        if directory:
            value += "/"
        return self._spec.match_file(value)


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _run_git_for_changes(local_root: Path, arguments: list[str]) -> bytes:
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=local_root,
            capture_output=True,
            check=False,
            env=_git_environment(),
            timeout=GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise FileSyncConfigError(
            "cannot use --git-changed because Git is not installed or is not on PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FileSyncConfigError(
            f"Git change detection timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s "
            f"for {local_root}"
        ) from exc
    except OSError as exc:
        raise FileSyncConfigError(
            f"cannot run Git change detection for {local_root}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise FileSyncConfigError(
            f"cannot use --git-changed for {local_root}: "
            f"{detail or f'git exited with {result.returncode}'}"
        )
    return result.stdout


def git_changed_paths(local_root: Path) -> frozenset[PurePosixPath]:
    root = Path(os.path.abspath(local_root))
    if not root.exists():
        raise FileSyncConfigError(f"local context does not exist: {root}")
    if not root.is_dir():
        raise FileSyncConfigError(f"local context is not a directory: {root}")

    top_level_output = _run_git_for_changes(root, ["rev-parse", "--show-toplevel"])
    top_level_value = top_level_output.removesuffix(b"\n").removesuffix(b"\r")
    if not top_level_value:
        raise FileSyncConfigError(f"Git returned an empty repository root for {root}")
    git_root = Path(os.fsdecode(top_level_value))
    try:
        context_relative = root.resolve().relative_to(git_root.resolve())
    except (OSError, ValueError) as exc:
        raise FileSyncConfigError(
            f"local context {root} is outside its Git repository {git_root}"
        ) from exc

    context_path = PurePosixPath(context_relative.as_posix())
    pathspec = context_relative.as_posix() or "."
    status_output = _run_git_for_changes(
        git_root,
        [
            "--literal-pathspecs",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            "--",
            pathspec,
        ],
    )

    changed: set[PurePosixPath] = set()
    for record in status_output.split(b"\0"):
        if not record:
            continue
        if len(record) < 4 or record[2:3] != b" ":
            raise FileSyncConfigError(
                f"cannot parse Git status entry for {root}: {record!r}"
            )
        if record[:2] == b"!!":
            continue
        repository_path = PurePosixPath(os.fsdecode(record[3:]))
        if repository_path.is_absolute() or ".." in repository_path.parts:
            raise FileSyncConfigError(
                f"Git returned an unsafe changed path for {root}: {repository_path}"
            )
        try:
            relative = repository_path.relative_to(context_path)
        except ValueError as exc:
            raise FileSyncConfigError(
                f"Git returned a changed path outside {root}: {repository_path}"
            ) from exc
        if relative.parts:
            changed.add(relative)
    return frozenset(changed)


def _filter_actions_for_git_paths(
    actions: list[SyncAction], changed_paths: frozenset[PurePosixPath]
) -> list[SyncAction]:
    changed_parents = frozenset(
        parent for changed_path in changed_paths for parent in changed_path.parents
    )
    filtered: list[SyncAction] = []
    for action in actions:
        if action.path in changed_paths:
            filtered.append(action)
            continue
        if action.path not in changed_parents:
            continue
        if action.kind in {
            ActionKind.CREATE_LOCAL_DIRECTORY,
            ActionKind.CREATE_REMOTE_DIRECTORY,
        } or (
            action.kind in {ActionKind.DELETE_LOCAL, ActionKind.DELETE_REMOTE}
            and action.existing is not None
            and action.existing.kind is EntryKind.DIRECTORY
        ):
            filtered.append(action)
    return filtered


def _scan_path_selection(
    paths: frozenset[PurePosixPath] | None,
) -> frozenset[PurePosixPath] | None:
    if paths is None:
        return None
    return paths.union(parent for path in paths for parent in path.parents)


def scan_local(
    root: Path,
    ignore: IgnoreMatcher,
    *,
    paths: frozenset[PurePosixPath] | None = None,
) -> Snapshot:
    if not root.exists():
        return Snapshot(False, {})
    if not root.is_dir():
        raise FileSyncError(f"local context is not a directory: {root}")

    entries: dict[PurePosixPath, FileEntry] = {}
    selected_paths = _scan_path_selection(paths)
    if selected_paths == frozenset():
        return Snapshot(True, entries)

    pending: list[tuple[Path, PurePosixPath | None]] = [(root, None)]
    while pending:
        directory, relative_dir = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as exc:
            raise FileSyncError(
                f"cannot list local directory {directory}: {exc}"
            ) from exc
        for child in children:
            relative = (
                PurePosixPath(child.name)
                if relative_dir is None
                else relative_dir / child.name
            )
            if selected_paths is not None and relative not in selected_paths:
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise FileSyncError(
                    f"cannot stat local path {child.path}: {exc}"
                ) from exc
            if stat.S_ISDIR(info.st_mode):
                kind = EntryKind.DIRECTORY
            elif stat.S_ISLNK(info.st_mode):
                kind = EntryKind.SYMLINK
            elif stat.S_ISREG(info.st_mode):
                kind = EntryKind.FILE
            else:
                continue
            if ignore.matches(relative, directory=kind is EntryKind.DIRECTORY):
                continue
            try:
                link_target = (
                    os.readlink(child.path) if kind is EntryKind.SYMLINK else None
                )
            except OSError as exc:
                raise FileSyncError(
                    f"cannot read local symlink {child.path}: {exc}"
                ) from exc
            entries[relative] = FileEntry(
                kind=kind,
                size=info.st_size,
                mtime=info.st_mtime,
                mode=stat.S_IMODE(info.st_mode),
                link_target=link_target,
            )
            if kind is EntryKind.DIRECTORY:
                pending.append((Path(child.path), relative))

    return Snapshot(True, entries)


def _ignored_bundle_path(path: PurePosixPath, ignore: IgnoreMatcher) -> bool:
    # Deleted paths may no longer have a type, and a negated child cannot make
    # an ignored parent traversable. Match the same boundary as tree scanning.
    return ignore.matches(path, directory=False) or any(
        ignore.matches(parent, directory=True)
        for parent in (path, *path.parents)
        if parent.parts
    )
