from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from hagency_cli.files.purge.models import (
    CACHEDIR_TAG_NAME,
    CACHEDIR_TAG_SIGNATURE,
    MIN_AGE_SECONDS,
    MONOREPO_INDICATORS,
    PROJECT_INDICATORS,
    SCAN_PRUNE_NAMES_CASEFOLD,
    Activity,
    PurgeIssue,
    _GitContext,
    _HardlinkEntry,
    _Identity,
)


def _identity(path: Path) -> _Identity:
    info = path.stat(follow_symlinks=False)
    if not info.st_ino:
        raise OSError(f"stable filesystem identity is unavailable for {path}")
    return _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def _is_reparse_stat(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _is_link_or_reparse(path: Path) -> bool:
    info = path.stat(follow_symlinks=False)
    return stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info)


def _has_link_or_reparse_component(path: Path) -> bool:
    absolute = path if path.is_absolute() else (Path.cwd() / path)
    current = Path(absolute.anchor)
    start = 1 if absolute.anchor else 0
    for part in absolute.parts[start:]:
        current /= part
        if _is_link_or_reparse(current):
            return True
    return False


def _same_identity(path: Path, expected: _Identity) -> bool:
    try:
        return _identity(path) == expected
    except OSError:
        return False


def _canonical_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _is_within(path: Path, root: Path, *, allow_equal: bool = False) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return allow_equal or bool(relative.parts)


def _expand_config_path(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith(("~/", "~\\")):
        return home / value[2:]
    return Path(value)


def _has_project_marker(directory: Path) -> bool:
    return any((directory / marker).exists() for marker in PROJECT_INDICATORS)


def _project_marker_state(directory: Path) -> tuple[bool, list[PurgeIssue]]:
    issues: list[PurgeIssue] = []
    for marker in PROJECT_INDICATORS:
        marker_path = directory / marker
        try:
            marker_path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_stat_failed",
                    marker_path,
                    f"could not inspect project marker: {exc}",
                )
            )
        else:
            return True, issues
    return False, issues


def _contains_project_marker(
    root: Path, max_depth: int = 2
) -> tuple[bool, list[PurgeIssue]]:
    issues: list[PurgeIssue] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        has_marker, marker_issues = _project_marker_state(directory)
        issues.extend(marker_issues)
        if has_marker:
            return True, issues
        if depth >= max_depth:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if (
                        entry.name.startswith(".")
                        or entry.name.casefold() in SCAN_PRUNE_NAMES_CASEFOLD
                    ):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False) and not _is_reparse_stat(
                            entry.stat(follow_symlinks=False)
                        ):
                            stack.append((Path(entry.path), depth + 1))
                    except OSError as exc:
                        issues.append(
                            PurgeIssue(
                                "discovery_stat_failed",
                                Path(entry.path),
                                f"could not inspect potential project directory: {exc}",
                            )
                        )
                        continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_scan_failed",
                    directory,
                    f"could not scan potential project container: {exc}",
                )
            )
    return False, issues


def _valid_cachedir_tag(directory: Path) -> bool:
    tag = directory / CACHEDIR_TAG_NAME
    try:
        info = tag.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or _is_reparse_stat(info):
            return False
        with tag.open("rb") as handle:
            return handle.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


def _find_project_root(candidate: Path, scan_root: Path) -> Path | None:
    current = candidate.parent
    project_root: Path | None = None
    while _is_within(current, scan_root, allow_equal=True):
        if any((current / marker).exists() for marker in MONOREPO_INDICATORS):
            return current
        if project_root is None and _has_project_marker(current):
            project_root = current
        if current == scan_root:
            break
        current = current.parent
    return project_root


def _is_dotnet_bin(path: Path) -> bool:
    parent = path.parent
    try:
        has_project = any(
            child.is_file()
            and child.suffix.lower() in {".csproj", ".fsproj", ".vbproj"}
            for child in parent.iterdir()
        )
    except OSError:
        return False
    return has_project and ((path / "Debug").is_dir() or (path / "Release").is_dir())


def _context_allows(path: Path, project_root: Path) -> bool:
    if path.name == "vendor" and not (path.parent / "composer.json").is_file():
        return False
    if path.name == "bin" and not _is_dotnet_bin(path):
        return False
    if path.name == "DerivedData":
        parts = path.parts
        for index in range(len(parts) - 3):
            if parts[index : index + 4] == (
                "Library",
                "Developer",
                "Xcode",
                "DerivedData",
            ):
                return False
    return _is_within(path, project_root)


def _git_context_from_marker(marker: Path) -> tuple[_GitContext | None, str | None]:
    try:
        info = marker.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"could not inspect {marker}: {exc}"
    if (
        stat.S_ISLNK(info.st_mode)
        or _is_reparse_stat(info)
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
    ):
        return None, f"unsafe Git marker: {marker}"
    if not info.st_ino:
        return None, f"stable Git marker identity is unavailable: {marker}"
    return (
        _GitContext(
            root=marker.parent.resolve(),
            marker_identity=_Identity(
                info.st_dev,
                info.st_ino,
                stat.S_IFMT(info.st_mode),
            ),
        ),
        None,
    )


def _discover_git_contexts(
    path: Path,
) -> tuple[tuple[_GitContext, ...], str | None]:
    contexts: dict[str, _GitContext] = {}
    current = path
    while True:
        context, error = _git_context_from_marker(current / ".git")
        if error is not None:
            return (), error
        if context is not None:
            contexts[_canonical_key(context.root)] = context
        if current.parent == current:
            break
        current = current.parent

    stack = [path]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                entry_list = list(entries)
        except OSError as exc:
            return (
                (),
                f"could not inspect nested Git repositories under {directory}: {exc}",
            )
        for entry in entry_list:
            entry_path = Path(entry.path)
            if entry.name.casefold() == ".git":
                context, error = _git_context_from_marker(entry_path)
                if error is not None:
                    return (), error
                if context is not None:
                    contexts[_canonical_key(context.root)] = context
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                return (
                    (),
                    f"could not inspect {entry_path} for nested Git repositories: {exc}",
                )
            if (
                stat.S_ISDIR(info.st_mode)
                and not stat.S_ISLNK(info.st_mode)
                and not _is_reparse_stat(info)
            ):
                stack.append(entry_path)

    return tuple(contexts[key] for key in sorted(contexts)), None


def _git_tracked_state(path: Path, git_root: Path) -> tuple[bool | None, str | None]:
    try:
        relative = path.relative_to(git_root).as_posix()
        git_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_environment["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            [
                "git",
                "--literal-pathspecs",
                "-C",
                str(git_root),
                "ls-files",
                "-z",
                "--",
                relative,
            ],
            capture_output=True,
            check=False,
            env=git_environment,
            timeout=10,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        return None, detail or f"git exited with {result.returncode}"
    return bool(result.stdout), None


def _measure_candidate(
    path: Path,
    now: float,
) -> tuple[int | None, Activity, str | None, tuple[_HardlinkEntry, ...]]:
    seen: set[tuple[int, int]] = set()
    hardlink_entries: list[_HardlinkEntry] = []
    total = 0
    newest = 0.0
    stack = [path]
    try:
        while stack:
            current = stack.pop()
            info = current.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or _is_reparse_stat(info):
                newest = max(newest, info.st_mtime)
                continue
            if stat.S_ISDIR(info.st_mode) and os.path.ismount(current):
                return (
                    None,
                    Activity.UNCERTAIN,
                    f"refusing to measure mount point at {current}",
                    (),
                )
            identity = (info.st_dev, info.st_ino)
            if not info.st_ino or identity not in seen:
                if info.st_ino:
                    seen.add(identity)
                blocks = getattr(info, "st_blocks", None)
                allocated = (
                    blocks * 512
                    if os.name != "nt" and blocks is not None
                    else info.st_size
                )
                total += allocated
                if info.st_ino and info.st_nlink > 1 and not stat.S_ISDIR(info.st_mode):
                    hardlink_entries.append(_HardlinkEntry(identity, allocated))
            newest = max(newest, info.st_mtime)
            if not stat.S_ISDIR(info.st_mode):
                continue
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    except OSError as exc:
        return (
            None,
            Activity.UNCERTAIN,
            f"could not measure {current}: {exc}",
            (),
        )

    activity = Activity.OLD if newest < now - MIN_AGE_SECONDS else Activity.RECENT
    return total, activity, None, tuple(hardlink_entries)
