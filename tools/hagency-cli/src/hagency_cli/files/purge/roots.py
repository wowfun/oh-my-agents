from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from hagency_cli.files.purge.inspection import (
    _canonical_key,
    _contains_project_marker,
    _expand_config_path,
    _has_link_or_reparse_component,
    _identity,
    _is_reparse_stat,
)
from hagency_cli.files.purge.models import (
    CLOUD_HOME_CHILD_PREFIXES,
    DEFAULT_ROOT_NAMES,
    EXCLUDED_HOME_CHILDREN,
    EXPLICIT_HIDDEN_ROOTS,
    PathsEditReport,
    PurgeIssue,
    PurgeRequest,
    _ConfiguredPaths,
    _DiscoveredRoots,
)


def purge_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    filename = "space-purge-paths"

    if platform == "win32":
        appdata = environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "Hagency" / filename
    elif platform == "darwin":
        return home / "Library" / "Application Support" / "Hagency" / filename
    else:
        xdg_config_home = environ.get("XDG_CONFIG_HOME")
        if xdg_config_home and Path(xdg_config_home).is_absolute():
            return Path(xdg_config_home) / "hagency" / filename

    return home / ".config" / "hagency" / filename


def _read_configured_paths(config_path: Path, home: Path) -> _ConfiguredPaths:
    try:
        info = config_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _ConfiguredPaths((), False, ())
    except OSError as exc:
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    f"could not inspect path config: {exc}",
                ),
            ),
        )
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _is_reparse_stat(info)
    ):
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    "path config must be a regular file, not a link or reparse point",
                ),
            ),
        )
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return _ConfiguredPaths(
            (),
            True,
            (
                PurgeIssue(
                    "config_read_failed",
                    config_path,
                    f"could not read path config: {exc}",
                ),
            ),
        )

    paths: list[Path] = []
    issues: list[PurgeIssue] = []
    has_entries = False
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        has_entries = True
        path = _expand_config_path(line, home)
        if not path.is_absolute():
            issues.append(
                PurgeIssue(
                    "config_path_not_absolute",
                    config_path,
                    f"line {number} must be an absolute or ~ path: {line}",
                )
            )
            continue
        paths.append(path)
    return _ConfiguredPaths(tuple(paths), has_entries, tuple(issues))


def _autodiscover_roots(
    home: Path, *, environ: Mapping[str, str] | None = None
) -> _DiscoveredRoots:
    environ = os.environ if environ is None else environ
    candidates = [home / name for name in DEFAULT_ROOT_NAMES]
    candidates.extend(home / relative for relative in EXPLICIT_HIDDEN_ROOTS)
    issues: list[PurgeIssue] = []
    excluded_names = {name.casefold() for name in EXCLUDED_HOME_CHILDREN}
    explicit_cloud_roots = {
        _canonical_key(Path(value).expanduser())
        for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
        if (value := environ.get(key))
    }
    try:
        with os.scandir(home) as entries:
            for entry in entries:
                folded_name = entry.name.casefold()
                if (
                    entry.name.startswith(".")
                    or folded_name in excluded_names
                    or folded_name.startswith(CLOUD_HOME_CHILD_PREFIXES)
                ):
                    continue
                path = Path(entry.path)
                if _canonical_key(path) in explicit_cloud_roots:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if _is_reparse_stat(entry.stat(follow_symlinks=False)):
                        continue
                    if os.path.ismount(path):
                        continue
                except OSError as exc:
                    issues.append(
                        PurgeIssue(
                            "discovery_stat_failed",
                            path,
                            f"could not inspect home directory entry: {exc}",
                        )
                    )
                    continue
                contains_marker, marker_issues = _contains_project_marker(path)
                issues.extend(marker_issues)
                if contains_marker:
                    candidates.append(path)
    except OSError as exc:
        return _DiscoveredRoots(
            (),
            (
                PurgeIssue(
                    "discovery_scan_failed",
                    home,
                    f"could not scan home directory: {exc}",
                ),
            ),
        )

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or _is_reparse_stat(info):
                continue
            resolved = path.resolve()
            key = _canonical_key(resolved)
        except FileNotFoundError:
            continue
        except OSError as exc:
            issues.append(
                PurgeIssue(
                    "discovery_stat_failed",
                    path,
                    f"could not inspect automatic purge root: {exc}",
                )
            )
            continue
        if key not in seen:
            seen.add(key)
            deduped.append(resolved)
    return _DiscoveredRoots(tuple(deduped), tuple(issues))


def _validate_roots(
    raw_paths: tuple[Path, ...], home: Path
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    roots: list[Path] = []
    issues: list[PurgeIssue] = []
    seen: set[tuple[int, int] | str] = set()
    resolved_home = home.resolve()

    for raw_path in raw_paths:
        path = raw_path if raw_path.is_absolute() else (Path.cwd() / raw_path)
        try:
            if any(
                ord(character) < 32 or ord(character) == 127 for character in str(path)
            ):
                raise ValueError("control characters are not allowed in purge roots")
            if _has_link_or_reparse_component(path):
                raise ValueError("symlink, junction, or reparse roots are not allowed")
            resolved = path.resolve(strict=True)
            if not resolved.is_dir():
                raise ValueError("not a directory")
            if resolved.parent == resolved or resolved == resolved_home:
                raise ValueError("filesystem root and home directory are protected")
            identity = _identity(resolved)
            key: tuple[int, int] | str = (
                (identity.device, identity.inode)
                if identity.inode
                else _canonical_key(resolved)
            )
        except (OSError, ValueError) as exc:
            issues.append(
                PurgeIssue("invalid_root", path, f"invalid purge root: {exc}")
            )
            continue
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)
    return tuple(roots), tuple(issues)


def _resolve_roots(
    request: PurgeRequest,
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    home = Path.home()
    if request.paths:
        return _validate_roots(request.paths, home)

    configured = _read_configured_paths(purge_config_path(home=home), home)
    discovered = (
        _DiscoveredRoots(configured.paths, ())
        if configured.has_entries
        else _autodiscover_roots(home)
    )
    raw_paths = discovered.paths
    roots, validation_issues = _validate_roots(raw_paths, home)
    return roots, (*configured.issues, *discovered.issues, *validation_issues)


def _write_config_template(config_path: Path) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    template = (
        "# Hagency project artifact purge paths\n"
        "# Add one absolute or ~ path per line.\n"
        "# Leave this file empty to use automatic discovery.\n"
        "#\n"
        "# ~/Projects\n"
        "# ~/Work/ClientA\n"
    )
    try:
        with config_path.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
        return


def _effective_config_roots(
    config_path: Path, home: Path
) -> tuple[tuple[Path, ...], tuple[PurgeIssue, ...]]:
    configured = _read_configured_paths(config_path, home)
    discovered = (
        _DiscoveredRoots(configured.paths, ())
        if configured.has_entries
        else _autodiscover_roots(home)
    )
    roots, issues = _validate_roots(discovered.paths, home)
    return roots, (*configured.issues, *discovered.issues, *issues)


def _split_editor_command(value: str) -> list[str]:
    command = shlex.split(value, posix=sys.platform != "win32")
    if sys.platform == "win32":
        command = [
            token[1:-1]
            if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}
            else token
            for token in command
        ]
    return command


def edit_purge_paths() -> PathsEditReport:
    home = Path.home()
    config_path = purge_config_path(home=home)
    before_roots, before_issues = _effective_config_roots(config_path, home)
    issues = list(before_issues)
    editor_value = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor_value:
        if sys.platform == "win32":
            editor_value = "notepad.exe"
        elif sys.platform == "darwin":
            editor_value = "open -W -t"
        else:
            editor_value = "vi"

    try:
        _write_config_template(config_path)
        editor_command = _split_editor_command(editor_value)
        if not editor_command:
            raise ValueError("editor command is empty")
        result = subprocess.run([*editor_command, str(config_path)], check=False)
        if result.returncode != 0:
            issues.append(
                PurgeIssue(
                    "editor_failed",
                    config_path,
                    f"editor exited with {result.returncode}",
                )
            )
    except (OSError, ValueError) as exc:
        issues.append(
            PurgeIssue(
                "editor_failed", config_path, f"could not edit path config: {exc}"
            )
        )

    after_roots, after_issues = _effective_config_roots(config_path, home)
    issues.extend(after_issues)
    return PathsEditReport(
        config_path=config_path,
        before_roots=before_roots,
        after_roots=after_roots,
        editor=editor_value,
        issues=tuple(issues),
    )


__all__ = ["edit_purge_paths", "purge_config_path"]
