from __future__ import annotations

import errno
import getpass
import hashlib
import io
import ipaddress
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, Self

from pathspec import GitIgnoreSpec

CONFIG_RELATIVE_PATH = Path(".vscode") / "sftp.json"
PROTECTED_CONFIG_PATTERN = "/.vscode/sftp.json"
TEMPORARY_CONFIG_SOURCE = "temporary endpoint"
CONTENT_CHUNK_SIZE = 128 * 1024
# Large working trees can take much longer to inspect than an SSH connection.
GIT_COMMAND_TIMEOUT_SECONDS = 120
BUNDLE_FORMAT = "hagency-sync-bundle"
BUNDLE_VERSION = 1
BUNDLE_MANIFEST_PATH = "manifest.json"
BUNDLE_MANIFEST_MAX_BYTES = 16 * 1024 * 1024
BUNDLE_PAYLOAD_PREFIX = "payload/"
DEFAULT_BUNDLE_FILENAME = "hgc-sync.zip"
DEFAULT_SFTP_CONFIG: dict[str, object] = {
    "name": "My Server",
    "host": "localhost",
    "protocol": "sftp",
    "port": 22,
    "username": "username",
    "remotePath": "/",
    "uploadOnSave": False,
    "useTempFile": False,
    "openSsh": False,
}


class FileSyncConfigError(ValueError):
    pass


class FileSyncUsageError(FileSyncConfigError):
    """Invalid sync arguments, detected before reading config or connecting."""


class FileSyncError(RuntimeError):
    pass


class SyncDirection(str, Enum):
    LOCAL_TO_REMOTE = "local-to-remote"
    REMOTE_TO_LOCAL = "remote-to-local"
    BOTH = "both"


class EntryKind(str, Enum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"


class ActionKind(str, Enum):
    COPY_TO_REMOTE = "copy-to-remote"
    COPY_TO_LOCAL = "copy-to-local"
    CREATE_REMOTE_DIRECTORY = "create-remote-directory"
    CREATE_LOCAL_DIRECTORY = "create-local-directory"
    DELETE_REMOTE = "delete-remote"
    DELETE_LOCAL = "delete-local"


class BundleMode(str, Enum):
    FULL = "full"
    GIT_PATCH = "git-patch"


@dataclass(frozen=True)
class SyncOptions:
    delete: bool = False
    skip_create: bool = False
    ignore_existing: bool = False
    update: bool = False


@dataclass(frozen=True)
class RemoteEndpoint:
    host: str
    remote_path: str
    username: str | None = None


@dataclass(frozen=True)
class LocalSyncSelection:
    project_root: Path
    local_root: Path
    selection: str
    ignore_patterns: tuple[str, ...]
    protected_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class FileEntry:
    kind: EntryKind
    size: int
    mtime: float
    mode: int | None = None
    link_target: str | None = None


@dataclass(frozen=True)
class Snapshot:
    exists: bool
    entries: dict[PurePosixPath, FileEntry]


@dataclass(frozen=True)
class SyncAction:
    kind: ActionKind
    path: PurePosixPath
    source: FileEntry | None = None
    existing: FileEntry | None = None


@dataclass(frozen=True)
class SFTPConfig:
    config_path: Path | None
    workspace_root: Path
    selection: str
    name: str
    local_root: Path
    remote_root: str
    host: str
    port: int | None
    username: str | None
    password: str | None
    private_key_path: Path | None
    passphrase: str | bool | None
    agent: str | None
    ssh_config_path: Path
    connect_timeout: float
    remote_time_offset: float
    file_perm: int | None
    dir_perm: int | None
    use_temp_file: bool
    open_ssh: bool
    sync_options: SyncOptions
    ignore_patterns: tuple[str, ...]
    protected_paths: tuple[PurePosixPath, ...]

    @property
    def source(self) -> str:
        return (
            str(self.config_path)
            if self.config_path is not None
            else TEMPORARY_CONFIG_SOURCE
        )

    @property
    def endpoint(self) -> str:
        user = f"{self.username}@" if self.username else ""
        host = f"[{self.host}]" if ":" in self.host else self.host
        port = f":{self.port}" if self.port is not None and self.port != 22 else ""
        return f"{user}{host}{port}:{self.remote_root}"


@dataclass(frozen=True)
class SyncReport:
    config: SFTPConfig
    direction: SyncDirection
    actions: tuple[SyncAction, ...]
    dry_run: bool

    def count(self, *kinds: ActionKind) -> int:
        return sum(action.kind in kinds for action in self.actions)


@dataclass(frozen=True)
class BundleEntry:
    path: PurePosixPath
    kind: EntryKind
    size: int
    mtime_ns: int
    mode: int
    sha256: str | None

    def as_file_entry(self) -> FileEntry:
        return FileEntry(
            kind=self.kind,
            size=self.size,
            mtime=self.mtime_ns / 1_000_000_000,
            mode=self.mode,
        )


@dataclass(frozen=True)
class BundleManifest:
    mode: BundleMode
    created_at: str
    entries: tuple[BundleEntry, ...]
    deletions: tuple[PurePosixPath, ...]
    ignore_patterns: tuple[str, ...]
    skipped_symlinks: tuple[PurePosixPath, ...]


@dataclass(frozen=True)
class PackReport:
    output_path: Path | None
    local_root: Path
    manifest: BundleManifest | None
    dry_run: bool


@dataclass(frozen=True)
class ApplyReport:
    bundle_path: Path
    target_root: Path
    manifest: BundleManifest
    actions: tuple[SyncAction, ...]
    dry_run: bool


class RemoteFileSystem(Protocol):
    def snapshot(
        self,
        ignore: IgnoreMatcher,
        *,
        paths: frozenset[PurePosixPath] | None = None,
    ) -> Snapshot: ...

    def equivalent_file_content(
        self, relative: PurePosixPath, local_root: Path
    ) -> bool: ...

    def ensure_root(self) -> None: ...

    def apply(self, action: SyncAction, local_root: Path) -> None: ...


RemoteFactory = Callable[[SFTPConfig], AbstractContextManager[RemoteFileSystem]]
Progress = Callable[[str], None]


@dataclass(frozen=True)
class _ContentDigest:
    raw: bytes
    normalized: bytes
    contains_nul: bool


def render_default_sftp_config() -> str:
    return json.dumps(DEFAULT_SFTP_CONFIG, indent=4) + "\n"


def initialize_sftp_config(
    project_root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> Path:
    emit = progress or (lambda _message: None)
    root = Path(os.path.abspath(project_root))
    if not root.exists():
        raise FileSyncConfigError(f"project directory does not exist: {root}")
    if not root.is_dir():
        raise FileSyncConfigError(f"project path is not a directory: {root}")

    config_path = root / CONFIG_RELATIVE_PATH
    config_dir = config_path.parent
    if os.path.lexists(config_dir) and not config_dir.is_dir():
        raise FileSyncConfigError(f"config directory is not a directory: {config_dir}")

    config_exists = os.path.lexists(config_path)
    if config_exists and config_path.is_dir():
        raise FileSyncConfigError(f"config path is not a file: {config_path}")
    if config_exists and not force:
        raise FileSyncConfigError(f"SFTP config already exists: {config_path}")

    content = render_default_sftp_config()
    action = "overwrite" if config_exists else "create"
    if dry_run:
        emit(f"Would {action} SFTP config: {config_path}")
        emit(content.rstrip())
        return config_path

    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        config_dir.mkdir(exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.hgc-", dir=config_dir
        )
        temporary_path = Path(temporary_name)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n")
        file_descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
    except OSError as exc:
        raise FileSyncError(f"cannot write SFTP config {config_path}: {exc}") from exc
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    emit(f"initialized SFTP config: {config_path}")
    return config_path


class IgnoreMatcher:
    def __init__(
        self,
        patterns: tuple[str, ...],
        protected_paths: tuple[PurePosixPath, ...] = (),
    ) -> None:
        self.patterns = patterns
        self.protected_paths = frozenset(protected_paths)
        self._protected_casefold = {
            protected.as_posix().casefold() for protected in protected_paths
        }
        self._spec = GitIgnoreSpec.from_lines(patterns)

    def matches(self, path: PurePosixPath, *, directory: bool) -> bool:
        if path.as_posix().casefold() in self._protected_casefold:
            return True
        value = path.as_posix()
        if directory:
            value += "/"
        return self._spec.match_file(value)


def _load_json(path: Path) -> object:
    if not path.exists():
        raise FileSyncConfigError(f"missing config: {path}")
    if not path.is_file():
        raise FileSyncConfigError(f"config is not a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FileSyncConfigError(f"cannot read {path}: {exc}") from exc


def _require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise FileSyncConfigError(f"{label} must be a JSON object")
    return value


def _selection_choices(configs: list[dict]) -> list[str]:
    choices: list[str] = []
    for index, config in enumerate(configs, start=1):
        name = config.get("name")
        base = name if isinstance(name, str) and name else f"config-{index}"
        choices.append(base)
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            choices.extend(f"{base}:{profile}" for profile in profiles)
    return choices


def _merge_profile(base: dict, profile_name: str) -> dict:
    profiles = _require_mapping(base.get("profiles", {}), "profiles")
    if profile_name not in profiles:
        raise FileSyncConfigError(f"unknown SFTP profile: {profile_name}")
    profile = _require_mapping(profiles[profile_name], f"profiles.{profile_name}")
    merged = {key: value for key, value in base.items() if key != "profiles"}
    if "ignore" in profile:
        base_ignore = merged.get("ignore") or []
        profile_ignore = profile.get("ignore") or []
        if not isinstance(base_ignore, list) or not isinstance(profile_ignore, list):
            raise FileSyncConfigError("ignore must be an array of strings")
        merged["ignore"] = [*base_ignore, *profile_ignore]
    merged.update({key: value for key, value in profile.items() if key != "ignore"})
    return merged


def _select_config(raw: object, profile: str | None) -> tuple[dict, str]:
    raw_configs = raw if isinstance(raw, list) else [raw]
    if not raw_configs:
        raise FileSyncConfigError("sftp.json must contain at least one config")
    configs = [
        _require_mapping(value, f"config[{index}]")
        for index, value in enumerate(raw_configs)
    ]
    choices = _selection_choices(configs)

    if profile is None:
        if len(configs) != 1:
            raise FileSyncConfigError(
                "multiple SFTP configs found; select one with --profile "
                f"({', '.join(choices)})"
            )
        selected = configs[0]
        name = selected.get("name")
        selection = name if isinstance(name, str) and name else "config-1"
        default_profile = selected.get("defaultProfile")
        if default_profile is not None:
            if not isinstance(default_profile, str) or not default_profile:
                raise FileSyncConfigError("defaultProfile must be a non-empty string")
            selected = _merge_profile(selected, default_profile)
            selection = f"{selection}:{default_profile}"
        return selected, selection

    matches: list[tuple[dict, str, str]] = []
    explicit_base, separator, explicit_nested = profile.partition(":")
    for index, config in enumerate(configs, start=1):
        name_value = config.get("name")
        name = (
            name_value
            if isinstance(name_value, str) and name_value
            else f"config-{index}"
        )
        if separator:
            if name == explicit_base:
                if explicit_nested:
                    profiles = config.get("profiles")
                    if isinstance(profiles, dict) and explicit_nested in profiles:
                        matches.append(
                            (
                                _merge_profile(config, explicit_nested),
                                profile,
                                profile,
                            )
                        )
                else:
                    matches.append((config, name, f"{name}:"))
            continue
        if name == profile:
            selected = config
            default_profile = config.get("defaultProfile")
            selection = name
            if default_profile is not None:
                if not isinstance(default_profile, str) or not default_profile:
                    raise FileSyncConfigError(
                        "defaultProfile must be a non-empty string"
                    )
                selected = _merge_profile(config, default_profile)
                selection = f"{name}:{default_profile}"
            matches.append((selected, selection, f"{name}:"))
        profiles = config.get("profiles")
        if isinstance(profiles, dict) and profile in profiles:
            nested_selection = f"{name}:{profile}"
            matches.append(
                (_merge_profile(config, profile), nested_selection, nested_selection)
            )

    if len(matches) == 1:
        selected, selection, _selector = matches[0]
        return selected, selection
    if not matches:
        raise FileSyncConfigError(
            f"unknown SFTP profile {profile!r}; choose from: {', '.join(choices)}"
        )
    raise FileSyncConfigError(
        f"ambiguous SFTP profile {profile!r}; use CONFIG: for a base config or "
        f"CONFIG:PROFILE for a nested profile "
        f"({', '.join(selector for _config, _selection, selector in matches)})"
    )


def _optional_string(config: dict, key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FileSyncConfigError(f"{key} must be a string")
    return value


def _bool_value(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise FileSyncConfigError(f"{key} must be true or false")
    return value


def _positive_int(config: dict, key: str) -> int | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FileSyncConfigError(f"{key} must be a positive integer")
    return value


def _permission(config: dict, key: str) -> int | None:
    value = config.get(key)
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FileSyncConfigError(f"{key} must be an octal permission such as 644")
    try:
        parsed = int(str(value), 8)
    except ValueError as exc:
        raise FileSyncConfigError(
            f"{key} must be an octal permission such as 644"
        ) from exc
    if parsed < 0 or parsed > 0o7777:
        raise FileSyncConfigError(f"{key} is outside the supported permission range")
    return parsed


def _resolve_local_path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(path))


def _normalize_remote_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return normalized or "."


def parse_remote_endpoint(value: str) -> RemoteEndpoint:
    if not value or value != value.strip():
        raise FileSyncConfigError(
            "remote endpoint must not be empty or have surrounding whitespace"
        )
    if any(character in value for character in ("\0", "\r", "\n")):
        raise FileSyncConfigError("remote endpoint contains an invalid character")

    username: str | None = None
    if "[" in value or "]" in value:
        match = re.fullmatch(
            r"(?:(?P<user>[^@:\s]+)@)?\[(?P<host>[^\[\]\s]+)\]:(?P<path>.*)", value
        )
        if match is None:
            raise FileSyncConfigError(
                "invalid bracketed remote endpoint; expected [IPv6]:path or "
                "user@[IPv6]:path"
            )
        username = match.group("user")
        host = match.group("host")
        remote_path = match.group("path")
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise FileSyncConfigError(
                f"invalid IPv6 address in remote endpoint: {host}"
            ) from exc
    else:
        possible_authority, possible_separator, _possible_path = value.rpartition(":")
        possible_host = possible_authority.rpartition("@")[2]
        if possible_separator:
            try:
                ipaddress.IPv6Address(possible_host)
            except ValueError:
                pass
            else:
                raise FileSyncConfigError(
                    "IPv6 addresses in remote endpoints must be bracketed"
                )
        authority, separator, remote_path = value.partition(":")
        if not separator:
            raise FileSyncConfigError(
                "remote endpoint must use [user@]host:path syntax"
            )
        if authority.count("@") > 1:
            raise FileSyncConfigError("remote endpoint has an invalid user or host")
        username_value, user_separator, host_value = authority.rpartition("@")
        if user_separator:
            username = username_value
            host = host_value
        else:
            host = authority
        if not host or any(character.isspace() for character in host):
            raise FileSyncConfigError("remote endpoint host must not be empty")
        if not re.fullmatch(r"[^:/@\[\]\s]+", host):
            raise FileSyncConfigError(
                "invalid remote endpoint host; bracket IPv6 addresses"
            )
        if username is not None and (
            not username or any(character.isspace() for character in username)
        ):
            raise FileSyncConfigError("remote endpoint username must not be empty")

    if not remote_path:
        raise FileSyncConfigError(
            "remote endpoint path must not be empty; use host:. for the home directory"
        )
    if remote_path.startswith("~/"):
        # SFTP starts in the authenticated user's home directory but does not
        # require the server to expand shell-style tildes.
        remote_path = remote_path[2:] or "."
        if remote_path.startswith("/"):
            raise FileSyncConfigError(
                "remote endpoint home-relative paths must start with exactly ~/"
            )
    elif remote_path.startswith("~"):
        raise FileSyncConfigError(
            "remote endpoint supports only . or ~/path for home-relative paths"
        )
    return RemoteEndpoint(
        host=host,
        remote_path=_normalize_remote_path(remote_path),
        username=username,
    )


def build_temporary_sftp_config(
    local_root: Path,
    endpoint: str | RemoteEndpoint,
    *,
    port: int | None = None,
    identity: Path | None = None,
    exclude: Sequence[str] = (),
    sync_options: SyncOptions | None = None,
) -> SFTPConfig:
    root = Path(os.path.abspath(local_root))
    parsed = parse_remote_endpoint(endpoint) if isinstance(endpoint, str) else endpoint
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise FileSyncConfigError("port must be between 1 and 65535")
    if any(not isinstance(pattern, str) for pattern in exclude):
        raise FileSyncConfigError("exclude patterns must be strings")

    identity_path = (
        _resolve_local_path(os.fspath(identity), Path.cwd())
        if identity is not None
        else None
    )
    # Built-ins remain last so user negations cannot re-include metadata or keys.
    patterns = (*exclude, ".git/", PROTECTED_CONFIG_PATTERN)
    return SFTPConfig(
        config_path=None,
        workspace_root=root,
        selection=TEMPORARY_CONFIG_SOURCE,
        name=TEMPORARY_CONFIG_SOURCE,
        local_root=root,
        remote_root=parsed.remote_path,
        host=parsed.host,
        port=port,
        username=parsed.username,
        password=None,
        private_key_path=identity_path,
        passphrase=None,
        agent=None,
        ssh_config_path=_resolve_local_path("~/.ssh/config", root),
        connect_timeout=10.0,
        remote_time_offset=0.0,
        file_perm=None,
        dir_perm=None,
        use_temp_file=False,
        open_ssh=False,
        sync_options=sync_options or SyncOptions(),
        ignore_patterns=patterns,
        protected_paths=(PurePosixPath(CONFIG_RELATIVE_PATH.as_posix()),),
    )


def load_sftp_config(workspace_root: Path, profile: str | None = None) -> SFTPConfig:
    root = Path(os.path.abspath(workspace_root))
    config_path = root / CONFIG_RELATIVE_PATH
    selected, selection = _select_config(_load_json(config_path), profile)

    name = _optional_string(selected, "name") or selection
    protocol = _optional_string(selected, "protocol") or "sftp"
    if protocol != "sftp":
        raise FileSyncConfigError(
            f"unsupported protocol {protocol!r}; file sync currently supports SFTP"
        )
    host = _optional_string(selected, "host")
    if not host:
        raise FileSyncConfigError("host is required")

    port = _positive_int(selected, "port")
    if port is not None and port > 65535:
        raise FileSyncConfigError("port must be between 1 and 65535")

    timeout_ms = selected.get("connectTimeout", 10_000)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int | float)
        or timeout_ms <= 0
    ):
        raise FileSyncConfigError("connectTimeout must be a positive number")

    offset = selected.get("remoteTimeOffsetInHours", 0)
    if isinstance(offset, bool) or not isinstance(offset, int | float):
        raise FileSyncConfigError("remoteTimeOffsetInHours must be a number")

    context = _optional_string(selected, "context") or ""
    local_root = _resolve_local_path(context, root)
    remote_path = _optional_string(selected, "remotePath") or "./"

    ignore = selected.get("ignore") or []
    if not isinstance(ignore, list) or any(
        not isinstance(item, str) for item in ignore
    ):
        raise FileSyncConfigError("ignore must be an array of strings")
    patterns = [*ignore]
    ignore_file = _optional_string(selected, "ignoreFile")
    if ignore_file:
        ignore_path = _resolve_local_path(ignore_file, root)
        if not ignore_path.is_file():
            raise FileSyncConfigError(f"ignoreFile not found: {ignore_path}")
        try:
            patterns.extend(ignore_path.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeError) as exc:
            raise FileSyncConfigError(
                f"cannot read ignoreFile {ignore_path}: {exc}"
            ) from exc
    # Keep this last so user negations cannot re-include the usual config path.
    # protected_paths also covers a config inside a custom context.
    patterns.append(PROTECTED_CONFIG_PATTERN)

    protected_paths: list[PurePosixPath] = [
        PurePosixPath(CONFIG_RELATIVE_PATH.as_posix())
    ]
    try:
        config_relative = config_path.relative_to(local_root)
    except ValueError:
        pass
    else:
        protected_paths.append(PurePosixPath(config_relative.as_posix()))

    sync_raw = selected.get("syncOption") or {}
    sync_mapping = _require_mapping(sync_raw, "syncOption")
    sync_options = SyncOptions(
        delete=_bool_value(sync_mapping, "delete"),
        skip_create=_bool_value(sync_mapping, "skipCreate"),
        ignore_existing=_bool_value(sync_mapping, "ignoreExisting"),
        update=_bool_value(sync_mapping, "update"),
    )

    private_key = _optional_string(selected, "privateKeyPath")
    passphrase = selected.get("passphrase")
    if (
        passphrase is not None
        and passphrase is not True
        and not isinstance(passphrase, str)
    ):
        raise FileSyncConfigError("passphrase must be a string or true")
    ssh_config = _optional_string(selected, "sshConfigPath") or "~/.ssh/config"
    use_temp_file = _bool_value(selected, "useTempFile")
    open_ssh = _bool_value(selected, "openSsh")
    if open_ssh and not use_temp_file:
        raise FileSyncConfigError("openSsh requires useTempFile to be true")

    return SFTPConfig(
        config_path=config_path,
        workspace_root=root,
        selection=selection,
        name=name,
        local_root=local_root,
        remote_root=_normalize_remote_path(remote_path),
        host=host,
        port=port,
        username=_optional_string(selected, "username"),
        password=_optional_string(selected, "password"),
        private_key_path=(
            _resolve_local_path(private_key, root) if private_key else None
        ),
        passphrase=passphrase,
        agent=_optional_string(selected, "agent"),
        ssh_config_path=_resolve_local_path(ssh_config, root),
        connect_timeout=float(timeout_ms) / 1000,
        remote_time_offset=float(offset) * 3600,
        file_perm=_permission(selected, "filePerm"),
        dir_perm=_permission(selected, "dirPerm"),
        use_temp_file=use_temp_file,
        open_ssh=open_ssh,
        sync_options=sync_options,
        ignore_patterns=tuple(patterns),
        protected_paths=tuple(dict.fromkeys(protected_paths)),
    )


def load_local_sync_selection(
    project_root: Path,
    *,
    profile: str | None = None,
    no_config: bool = False,
    exclude: Sequence[str] = (),
    output_path: Path | None = None,
) -> LocalSyncSelection:
    root = Path(os.path.abspath(project_root))
    config_path = root / CONFIG_RELATIVE_PATH
    if no_config and profile is not None:
        raise FileSyncConfigError("--no-config and --profile are mutually exclusive")
    if any(not isinstance(pattern, str) for pattern in exclude):
        raise FileSyncConfigError("exclude patterns must be strings")

    patterns: list[str] = []
    config_protected_patterns: list[str] = []
    protected_paths: list[PurePosixPath] = [
        PurePosixPath(CONFIG_RELATIVE_PATH.as_posix())
    ]
    selection = "standalone"
    local_root = root

    if not no_config and os.path.lexists(config_path):
        selected, selection = _select_config(_load_json(config_path), profile)
        context = _optional_string(selected, "context") or ""
        local_root = _resolve_local_path(context, root)
        ignore = selected.get("ignore") or []
        if not isinstance(ignore, list) or any(
            not isinstance(item, str) for item in ignore
        ):
            raise FileSyncConfigError("ignore must be an array of strings")
        patterns.extend(ignore)
        ignore_file = _optional_string(selected, "ignoreFile")
        if ignore_file:
            ignore_path = _resolve_local_path(ignore_file, root)
            if not ignore_path.is_file():
                raise FileSyncConfigError(f"ignoreFile not found: {ignore_path}")
            try:
                patterns.extend(
                    ignore_path.read_text(encoding="utf-8-sig").splitlines()
                )
            except (OSError, UnicodeError) as exc:
                raise FileSyncConfigError(
                    f"cannot read ignoreFile {ignore_path}: {exc}"
                ) from exc
        try:
            config_relative = config_path.relative_to(local_root)
        except ValueError:
            pass
        else:
            relative_config_path = PurePosixPath(config_relative.as_posix())
            protected_paths.append(relative_config_path)
            config_protected_patterns.append(f"/{relative_config_path.as_posix()}")
    elif profile is not None:
        raise FileSyncConfigError(
            f"cannot use --profile without SFTP config: {config_path}"
        )

    patterns.extend(exclude)
    output_protected_patterns: list[str] = []
    if output_path is not None:
        absolute_output = Path(os.path.abspath(output_path))
        try:
            output_relative = absolute_output.relative_to(local_root)
        except ValueError:
            pass
        else:
            if output_relative.parts:
                relative_output_path = PurePosixPath(output_relative.as_posix())
                protected_paths.append(relative_output_path)
                output_protected_patterns.append(f"/{relative_output_path.as_posix()}")

    # Keep hard exclusions last so negation rules cannot re-include them.
    patterns.extend(
        (
            ".git/",
            PROTECTED_CONFIG_PATTERN,
            *config_protected_patterns,
            *output_protected_patterns,
        )
    )

    return LocalSyncSelection(
        project_root=root,
        local_root=local_root,
        selection=selection,
        ignore_patterns=tuple(patterns),
        protected_paths=tuple(dict.fromkeys(protected_paths)),
    )


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


def _content_digest(stream: BinaryIO) -> _ContentDigest:
    raw_digest = hashlib.sha256()
    normalized_digest = hashlib.sha256()
    contains_nul = False
    pending_carriage_return = False

    while True:
        chunk = stream.read(CONTENT_CHUNK_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise FileSyncError("file content reader returned non-binary data")
        raw_digest.update(chunk)
        contains_nul = contains_nul or b"\0" in chunk
        if contains_nul:
            # A binary comparison uses only the raw digest, including later chunks.
            pending_carriage_return = False
            continue
        if pending_carriage_return:
            chunk = b"\r" + chunk
            pending_carriage_return = False
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
            pending_carriage_return = True
        normalized_digest.update(chunk.replace(b"\r\n", b"\n"))

    if pending_carriage_return:
        normalized_digest.update(b"\r")
    return _ContentDigest(
        raw=raw_digest.digest(),
        normalized=normalized_digest.digest(),
        contains_nul=contains_nul,
    )


def _content_digests_equal(local: _ContentDigest, remote: _ContentDigest) -> bool:
    if local.contains_nul or remote.contains_nul:
        return local.raw == remote.raw
    return local.normalized == remote.normalized


def _content_sizes_may_match(left: int, right: int) -> bool:
    # CRLF normalization can at most halve a file's size. A plain inequality
    # would incorrectly classify equivalent LF/CRLF text as different.
    return max(left, right) <= 2 * min(left, right)


def _remove_equivalent_file_actions(
    actions: list[SyncAction], remote: RemoteFileSystem, local_root: Path
) -> list[SyncAction]:
    filtered: list[SyncAction] = []
    for action in actions:
        compares_regular_files = (
            action.kind in {ActionKind.COPY_TO_LOCAL, ActionKind.COPY_TO_REMOTE}
            and action.source is not None
            and action.source.kind is EntryKind.FILE
            and action.existing is not None
            and action.existing.kind is EntryKind.FILE
            and _content_sizes_may_match(action.source.size, action.existing.size)
        )
        if compares_regular_files and remote.equivalent_file_content(
            action.path, local_root
        ):
            continue
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


def _is_changed(source: FileEntry, target: FileEntry) -> bool:
    if source.kind is not target.kind:
        return True
    if source.kind is EntryKind.DIRECTORY:
        return False
    if source.kind is EntryKind.SYMLINK:
        return source.link_target != target.link_target
    return int(source.mtime) != int(target.mtime) or source.size != target.size


def _copy_action(
    path: PurePosixPath,
    source: FileEntry,
    existing: FileEntry | None,
    *,
    source_is_local: bool,
) -> SyncAction:
    if source.kind is EntryKind.DIRECTORY:
        kind = (
            ActionKind.CREATE_REMOTE_DIRECTORY
            if source_is_local
            else ActionKind.CREATE_LOCAL_DIRECTORY
        )
    else:
        kind = (
            ActionKind.COPY_TO_REMOTE if source_is_local else ActionKind.COPY_TO_LOCAL
        )
    return SyncAction(kind=kind, path=path, source=source, existing=existing)


def _forced_side(
    path: PurePosixPath, forced: dict[PurePosixPath, bool | None]
) -> bool | None | str:
    for parent in path.parents:
        if parent in forced:
            return forced[parent]
    return "unforced"


def _sort_actions(actions: list[SyncAction]) -> list[SyncAction]:
    delete_kinds = {ActionKind.DELETE_LOCAL, ActionKind.DELETE_REMOTE}
    directory_kinds = {
        ActionKind.CREATE_LOCAL_DIRECTORY,
        ActionKind.CREATE_REMOTE_DIRECTORY,
    }

    def key(action: SyncAction) -> tuple[int, int, str]:
        depth = len(action.path.parts)
        if action.kind in delete_kinds:
            return (0, -depth, action.path.as_posix())
        if action.kind in directory_kinds:
            return (1, depth, action.path.as_posix())
        return (2, depth, action.path.as_posix())

    return sorted(actions, key=key)


def build_sync_plan(
    direction: SyncDirection,
    local: Snapshot,
    remote: Snapshot,
    options: SyncOptions,
) -> list[SyncAction]:
    if direction is SyncDirection.LOCAL_TO_REMOTE and not local.exists:
        raise FileSyncError("local context does not exist")
    if direction is SyncDirection.REMOTE_TO_LOCAL and not remote.exists:
        raise FileSyncError("remote path does not exist")
    if direction is SyncDirection.BOTH and not local.exists and not remote.exists:
        raise FileSyncError("neither the local context nor remote path exists")

    actions: list[SyncAction] = []
    root_path = PurePosixPath(".")
    root_entry = FileEntry(EntryKind.DIRECTORY, size=0, mtime=0)
    if (
        direction in {SyncDirection.LOCAL_TO_REMOTE, SyncDirection.BOTH}
        and local.exists
        and not remote.exists
    ):
        actions.append(_copy_action(root_path, root_entry, None, source_is_local=True))
    elif (
        direction in {SyncDirection.REMOTE_TO_LOCAL, SyncDirection.BOTH}
        and remote.exists
        and not local.exists
    ):
        actions.append(_copy_action(root_path, root_entry, None, source_is_local=False))

    forced: dict[PurePosixPath, bool | None] = {}
    paths = sorted(
        local.entries.keys() | remote.entries.keys(),
        key=lambda path: (len(path.parts), path.as_posix()),
    )

    for path in paths:
        forced_source = _forced_side(path, forced)
        if forced_source != "unforced":
            if forced_source is None:
                continue
            source = (
                local.entries.get(path) if forced_source else remote.entries.get(path)
            )
            if source is not None:
                actions.append(
                    _copy_action(path, source, None, source_is_local=forced_source)
                )
            continue

        local_entry = local.entries.get(path)
        remote_entry = remote.entries.get(path)

        if direction is SyncDirection.BOTH:
            if local_entry is None and remote_entry is not None:
                if not options.skip_create:
                    actions.append(
                        _copy_action(path, remote_entry, None, source_is_local=False)
                    )
                continue
            if remote_entry is None and local_entry is not None:
                if not options.skip_create:
                    actions.append(
                        _copy_action(path, local_entry, None, source_is_local=True)
                    )
                continue
            if local_entry is None or remote_entry is None:
                continue
            if local_entry.kind is not remote_entry.kind:
                if options.ignore_existing:
                    forced[path] = None
                    continue
                source_is_local = local_entry.mtime >= remote_entry.mtime
                source = local_entry if source_is_local else remote_entry
                target = remote_entry if source_is_local else local_entry
                actions.append(
                    _copy_action(path, source, target, source_is_local=source_is_local)
                )
                forced[path] = source_is_local
                continue
            if (
                local_entry.kind is not EntryKind.DIRECTORY
                and not options.ignore_existing
                and _is_changed(local_entry, remote_entry)
            ):
                source_is_local = local_entry.mtime >= remote_entry.mtime
                source = local_entry if source_is_local else remote_entry
                target = remote_entry if source_is_local else local_entry
                actions.append(
                    _copy_action(path, source, target, source_is_local=source_is_local)
                )
            continue

        source_is_local = direction is SyncDirection.LOCAL_TO_REMOTE
        source_entry = local_entry if source_is_local else remote_entry
        target_entry = remote_entry if source_is_local else local_entry
        delete_kind = (
            ActionKind.DELETE_REMOTE if source_is_local else ActionKind.DELETE_LOCAL
        )

        if source_entry is None and target_entry is not None:
            if options.delete:
                actions.append(
                    SyncAction(kind=delete_kind, path=path, existing=target_entry)
                )
            continue
        if source_entry is None:
            continue
        if target_entry is None:
            if not options.skip_create:
                actions.append(
                    _copy_action(
                        path, source_entry, None, source_is_local=source_is_local
                    )
                )
            continue
        if source_entry.kind is not target_entry.kind:
            if options.ignore_existing or (
                options.update and source_entry.mtime <= target_entry.mtime
            ):
                forced[path] = None
                continue
            actions.append(
                _copy_action(
                    path, source_entry, target_entry, source_is_local=source_is_local
                )
            )
            forced[path] = source_is_local
            continue
        if source_entry.kind is EntryKind.DIRECTORY or options.ignore_existing:
            continue
        if options.update and source_entry.mtime <= target_entry.mtime:
            continue
        if _is_changed(source_entry, target_entry):
            actions.append(
                _copy_action(
                    path, source_entry, target_entry, source_is_local=source_is_local
                )
            )

    return _sort_actions(actions)


def format_action(action: SyncAction, *, dry_run: bool) -> str:
    verbs = {
        ActionKind.COPY_TO_REMOTE: "upload",
        ActionKind.COPY_TO_LOCAL: "download",
        ActionKind.CREATE_REMOTE_DIRECTORY: "create remote directory",
        ActionKind.CREATE_LOCAL_DIRECTORY: "create local directory",
        ActionKind.DELETE_REMOTE: "delete remote",
        ActionKind.DELETE_LOCAL: "delete local",
    }
    prefix = "would " if dry_run else ""
    suffix = (
        " symlink" if action.source and action.source.kind is EntryKind.SYMLINK else ""
    )
    return f"{prefix}{verbs[action.kind]}{suffix}: {action.path.as_posix()}"


def _remove_local_for_replace(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _delete_local(path: Path, entry: FileEntry | None) -> None:
    try:
        if entry is not None and entry.kind is EntryKind.DIRECTORY:
            path.rmdir()
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        if (
            entry is not None
            and entry.kind is EntryKind.DIRECTORY
            and exc.errno
            in {
                errno.ENOTEMPTY,
                errno.EEXIST,
            }
        ):
            return
        raise


def _bundle_payload_path(path: PurePosixPath) -> str:
    return f"{BUNDLE_PAYLOAD_PREFIX}{path.as_posix()}"


def _bundle_manifest_payload(manifest: BundleManifest) -> bytes:
    entries = [
        {
            "path": entry.path.as_posix(),
            "kind": entry.kind.value,
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "mode": entry.mode,
            "sha256": entry.sha256,
        }
        for entry in manifest.entries
    ]
    payload = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "mode": manifest.mode.value,
        "created_at": manifest.created_at,
        "entries": entries,
        "deletions": [path.as_posix() for path in manifest.deletions],
        "ignore": list(manifest.ignore_patterns),
        "skipped_symlinks": [path.as_posix() for path in manifest.skipped_symlinks],
    }
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > BUNDLE_MANIFEST_MAX_BYTES:
        raise FileSyncConfigError(
            f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
        )
    return encoded


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _same_file_version(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_dev == after.st_dev
        and before.st_ino == after.st_ino
        and before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and stat.S_IFMT(before.st_mode) == stat.S_IFMT(after.st_mode)
    )


def _read_stable_local_file(
    path: Path, destination: BinaryIO | None = None
) -> tuple[os.stat_result, str]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise FileSyncError(f"bundle source is no longer a regular file: {path}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not _same_file_version(before, opened):
                raise FileSyncError(f"bundle source changed before reading: {path}")
            while True:
                chunk = source.read(CONTENT_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                if destination is not None:
                    destination.write(chunk)
            after_read = os.fstat(source.fileno())
        after_path = path.lstat()
    except FileSyncError:
        raise
    except OSError as exc:
        raise FileSyncError(f"cannot read bundle source {path}: {exc}") from exc
    if not _same_file_version(opened, after_read) or not _same_file_version(
        after_read, after_path
    ):
        raise FileSyncError(f"bundle source changed while reading: {path}")
    return after_read, digest.hexdigest()


def _bundle_candidate_paths(
    local_root: Path,
    snapshot: Snapshot,
    ignore: IgnoreMatcher,
    changed_paths: frozenset[PurePosixPath] | None,
) -> tuple[
    tuple[PurePosixPath, ...],
    tuple[PurePosixPath, ...],
    tuple[PurePosixPath, ...],
]:
    if changed_paths is None:
        selected = {
            path
            for path, entry in snapshot.entries.items()
            if entry.kind is not EntryKind.SYMLINK
        }
        skipped = {
            path
            for path, entry in snapshot.entries.items()
            if entry.kind is EntryKind.SYMLINK
        }
        return (
            tuple(
                sorted(selected, key=lambda path: (len(path.parts), path.as_posix()))
            ),
            (),
            tuple(sorted(skipped, key=lambda path: path.as_posix())),
        )

    selected: set[PurePosixPath] = set()
    deletions: set[PurePosixPath] = set()
    skipped: set[PurePosixPath] = set()
    for path in changed_paths:
        if ignore.matches(path, directory=False) or ignore.matches(
            path, directory=True
        ):
            continue
        entry = snapshot.entries.get(path)
        if entry is None:
            local_path = local_root.joinpath(*path.parts)
            if os.path.lexists(local_path):
                raise FileSyncError(
                    f"Git changed path has an unsupported local type: {local_path}"
                )
            deletions.add(path)
            continue
        if entry.kind is EntryKind.SYMLINK:
            skipped.add(path)
            continue
        selected.add(path)

    for path in tuple(selected):
        for parent in path.parents:
            parent_entry = snapshot.entries.get(parent)
            if parent_entry is not None and parent_entry.kind is EntryKind.DIRECTORY:
                selected.add(parent)

    return (
        tuple(sorted(selected, key=lambda path: (len(path.parts), path.as_posix()))),
        tuple(sorted(deletions, key=lambda path: path.as_posix())),
        tuple(sorted(skipped, key=lambda path: path.as_posix())),
    )


def _directory_bundle_entry(local_root: Path, path: PurePosixPath) -> BundleEntry:
    local_path = local_root.joinpath(*path.parts)
    try:
        info = local_path.lstat()
    except OSError as exc:
        raise FileSyncError(f"cannot stat bundle source {local_path}: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise FileSyncError(f"bundle source is no longer a directory: {local_path}")
    return BundleEntry(
        path=path,
        kind=EntryKind.DIRECTORY,
        size=0,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        sha256=None,
    )


def _file_bundle_entry(
    local_root: Path,
    path: PurePosixPath,
    archive: zipfile.ZipFile | None,
) -> BundleEntry:
    local_path = local_root.joinpath(*path.parts)
    if archive is None:
        info, digest = _read_stable_local_file(local_path)
    else:
        with archive.open(
            _zip_info(_bundle_payload_path(path)), "w", force_zip64=True
        ) as destination:
            info, digest = _read_stable_local_file(local_path, destination)
    return BundleEntry(
        path=path,
        kind=EntryKind.FILE,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        sha256=digest,
    )


def _build_bundle_manifest(
    selection: LocalSyncSelection,
    snapshot: Snapshot,
    selected_paths: tuple[PurePosixPath, ...],
    deletions: tuple[PurePosixPath, ...],
    skipped_symlinks: tuple[PurePosixPath, ...],
    mode: BundleMode,
    archive: zipfile.ZipFile | None,
    emit: Progress,
    *,
    dry_run: bool,
) -> BundleManifest:
    entries: list[BundleEntry] = []
    for path in selected_paths:
        source = snapshot.entries[path]
        if source.kind is EntryKind.DIRECTORY:
            entry = _directory_bundle_entry(selection.local_root, path)
        elif source.kind is EntryKind.FILE:
            entry = _file_bundle_entry(selection.local_root, path, archive)
        else:
            continue
        entries.append(entry)
        prefix = "would pack" if dry_run else "pack"
        emit(f"{prefix}: {path.as_posix()}")

    created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return BundleManifest(
        mode=mode,
        created_at=created_at,
        entries=tuple(entries),
        deletions=deletions,
        ignore_patterns=selection.ignore_patterns,
        skipped_symlinks=skipped_symlinks,
    )


def pack_sync_bundle(
    project_root: Path,
    *,
    profile: str | None = None,
    no_config: bool = False,
    output_path: Path | None = None,
    force: bool = False,
    git_changed: bool = False,
    exclude: Sequence[str] = (),
    dry_run: bool = False,
    progress: Progress | None = None,
) -> PackReport:
    emit = progress or (lambda _message: None)
    output = Path(
        os.path.abspath(output_path or (Path.cwd() / DEFAULT_BUNDLE_FILENAME))
    )
    selection = load_local_sync_selection(
        project_root,
        profile=profile,
        no_config=no_config,
        exclude=exclude,
        output_path=output,
    )
    emit(f"source: {selection.local_root}")
    emit(f"selection: {selection.selection}")

    if not selection.local_root.exists():
        raise FileSyncConfigError(
            f"local context does not exist: {selection.local_root}"
        )
    if not selection.local_root.is_dir():
        raise FileSyncConfigError(
            f"local context is not a directory: {selection.local_root}"
        )
    ignore = IgnoreMatcher(selection.ignore_patterns, selection.protected_paths)
    changed_paths = git_changed_paths(selection.local_root) if git_changed else None
    snapshot = scan_local(selection.local_root, ignore, paths=changed_paths)
    selected_paths, deletions, skipped_symlinks = _bundle_candidate_paths(
        selection.local_root, snapshot, ignore, changed_paths
    )
    mode = BundleMode.GIT_PATCH if git_changed else BundleMode.FULL

    for path in skipped_symlinks:
        emit(f"warning: skipped symlink: {path.as_posix()}")
    if git_changed and not selected_paths and not deletions:
        emit("no packable Git changes")
        return PackReport(
            output_path=None,
            local_root=selection.local_root,
            manifest=None,
            dry_run=dry_run,
        )

    output_parent = output.parent
    if not output_parent.exists():
        raise FileSyncConfigError(f"output directory does not exist: {output_parent}")
    if not output_parent.is_dir():
        raise FileSyncConfigError(f"output parent is not a directory: {output_parent}")
    output_exists = os.path.lexists(output)
    if output_exists and output.is_dir():
        raise FileSyncConfigError(f"bundle output is a directory: {output}")
    if output_exists and not force:
        raise FileSyncConfigError(f"bundle output already exists: {output}")

    if dry_run:
        manifest = _build_bundle_manifest(
            selection,
            snapshot,
            selected_paths,
            deletions,
            skipped_symlinks,
            mode,
            None,
            emit,
            dry_run=True,
        )
        _bundle_manifest_payload(manifest)
        action = "overwrite" if output_exists else "create"
        emit(f"would {action} bundle: {output}")
        return PackReport(
            output_path=output,
            local_root=selection.local_root,
            manifest=manifest,
            dry_run=True,
        )

    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.hgc-", dir=output_parent
        )
        os.close(file_descriptor)
        file_descriptor = -1
        temporary_path = Path(temporary_name)
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            allowZip64=True,
        ) as archive:
            manifest = _build_bundle_manifest(
                selection,
                snapshot,
                selected_paths,
                deletions,
                skipped_symlinks,
                mode,
                archive,
                emit,
                dry_run=False,
            )
            archive.writestr(
                _zip_info(BUNDLE_MANIFEST_PATH),
                _bundle_manifest_payload(manifest),
            )
        with temporary_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except (FileSyncConfigError, FileSyncError):
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileSyncError(f"cannot write sync bundle {output}: {exc}") from exc
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    emit(f"created bundle: {output}")
    return PackReport(
        output_path=output,
        local_root=selection.local_root,
        manifest=manifest,
        dry_run=False,
    )


_BUNDLE_MANIFEST_KEYS = frozenset(
    {
        "format",
        "version",
        "mode",
        "created_at",
        "entries",
        "deletions",
        "ignore",
        "skipped_symlinks",
    }
)
_BUNDLE_ENTRY_KEYS = frozenset({"path", "kind", "size", "mtime_ns", "mode", "sha256"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def _strict_mapping_keys(value: object, expected: frozenset[str], label: str) -> dict:
    mapping = _require_mapping(value, label)
    actual = frozenset(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise FileSyncConfigError(f"{label} has invalid fields: {'; '.join(details)}")
    return mapping


def _parse_bundle_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise FileSyncConfigError(f"{label} must be a non-empty string")
    if any(character in value for character in ("\\", "\0", "\r", "\n")):
        raise FileSyncConfigError(f"{label} contains an invalid character")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.fullmatch(r"[A-Za-z]:.*", path.parts[0]) is not None
    ):
        raise FileSyncConfigError(f"{label} is not a safe relative path: {value}")
    return path


def _validate_windows_bundle_path(path: PurePosixPath) -> None:
    if os.name != "nt":
        return
    for part in path.parts:
        if (
            any(character in '<>:"|?*' or ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise FileSyncConfigError(
                f"bundle path is not valid on Windows: {path.as_posix()}"
            )


def _parse_bundle_manifest(raw: object) -> BundleManifest:
    manifest = _strict_mapping_keys(raw, _BUNDLE_MANIFEST_KEYS, "manifest")
    if manifest["format"] != BUNDLE_FORMAT:
        raise FileSyncConfigError("unsupported sync bundle format")
    version = manifest["version"]
    if isinstance(version, bool) or version != BUNDLE_VERSION:
        raise FileSyncConfigError(f"unsupported sync bundle version: {version!r}")
    try:
        mode = BundleMode(manifest["mode"])
    except (TypeError, ValueError) as exc:
        raise FileSyncConfigError(
            f"unsupported sync bundle mode: {manifest['mode']!r}"
        ) from exc

    created_at = manifest["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise FileSyncConfigError("manifest.created_at must be a UTC timestamp")
    try:
        parsed_time = datetime.fromisoformat(created_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise FileSyncConfigError(
            "manifest.created_at must be a UTC timestamp"
        ) from exc
    if parsed_time.utcoffset() != UTC.utcoffset(parsed_time):
        raise FileSyncConfigError("manifest.created_at must be a UTC timestamp")

    entries_raw = manifest["entries"]
    if not isinstance(entries_raw, list):
        raise FileSyncConfigError("manifest.entries must be an array")
    entries: list[BundleEntry] = []
    entry_paths: set[PurePosixPath] = set()
    for index, raw_entry in enumerate(entries_raw):
        entry = _strict_mapping_keys(
            raw_entry, _BUNDLE_ENTRY_KEYS, f"manifest.entries[{index}]"
        )
        path = _parse_bundle_path(entry["path"], f"manifest.entries[{index}].path")
        _validate_windows_bundle_path(path)
        if path in entry_paths:
            raise FileSyncConfigError(f"duplicate bundle entry path: {path}")
        entry_paths.add(path)
        try:
            kind = EntryKind(entry["kind"])
        except (TypeError, ValueError) as exc:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].kind must be file or directory"
            ) from exc
        if kind not in {EntryKind.FILE, EntryKind.DIRECTORY}:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].kind must be file or directory"
            )
        size = entry["size"]
        mtime_ns = entry["mtime_ns"]
        entry_mode = entry["mode"]
        digest = entry["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].size must be a non-negative integer"
            )
        if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
            raise FileSyncConfigError(
                f"manifest.entries[{index}].mtime_ns must be an integer"
            )
        if (
            isinstance(entry_mode, bool)
            or not isinstance(entry_mode, int)
            or not 0 <= entry_mode <= 0o7777
        ):
            raise FileSyncConfigError(f"manifest.entries[{index}].mode is invalid")
        if kind is EntryKind.FILE:
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise FileSyncConfigError(
                    f"manifest.entries[{index}].sha256 is invalid"
                )
        elif size != 0 or digest is not None:
            raise FileSyncConfigError(
                f"manifest.entries[{index}] has invalid directory metadata"
            )
        entries.append(
            BundleEntry(
                path=path,
                kind=kind,
                size=size,
                mtime_ns=mtime_ns,
                mode=entry_mode,
                sha256=digest,
            )
        )

    entries_by_path = {entry.path: entry for entry in entries}
    for entry in entries:
        for parent in entry.path.parents:
            if parent == PurePosixPath("."):
                continue
            parent_entry = entries_by_path.get(parent)
            if parent_entry is None:
                raise FileSyncConfigError(
                    f"bundle entry is missing parent directory: {parent}"
                )
            if parent_entry.kind is not EntryKind.DIRECTORY:
                raise FileSyncConfigError(
                    f"bundle file path is the parent of another entry: {parent}"
                )

    def parse_path_array(key: str) -> tuple[PurePosixPath, ...]:
        values = manifest[key]
        if not isinstance(values, list):
            raise FileSyncConfigError(f"manifest.{key} must be an array")
        paths = tuple(
            _parse_bundle_path(value, f"manifest.{key}[{index}]")
            for index, value in enumerate(values)
        )
        if len(set(paths)) != len(paths):
            raise FileSyncConfigError(f"manifest.{key} contains duplicate paths")
        for path in paths:
            _validate_windows_bundle_path(path)
        return paths

    deletions = parse_path_array("deletions")
    skipped_symlinks = parse_path_array("skipped_symlinks")
    if mode is BundleMode.FULL and deletions:
        raise FileSyncConfigError("full sync bundles cannot contain deletion markers")
    if entry_paths.intersection(deletions):
        raise FileSyncConfigError("bundle entries and deletions overlap")
    if entry_paths.intersection(skipped_symlinks) or set(deletions).intersection(
        skipped_symlinks
    ):
        raise FileSyncConfigError("bundle skipped symlinks overlap managed paths")

    ignore = manifest["ignore"]
    if not isinstance(ignore, list) or any(
        not isinstance(pattern, str) for pattern in ignore
    ):
        raise FileSyncConfigError("manifest.ignore must be an array of strings")

    all_paths = [*entry_paths, *deletions, *skipped_symlinks]
    portable_paths: dict[str, PurePosixPath] = {}
    for path in all_paths:
        key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        previous = portable_paths.get(key)
        if previous is not None and previous != path:
            raise FileSyncConfigError(
                f"bundle paths collide across platforms: {previous} and {path}"
            )
        portable_paths[key] = path

    return BundleManifest(
        mode=mode,
        created_at=created_at,
        entries=tuple(entries),
        deletions=deletions,
        ignore_patterns=tuple(ignore),
        skipped_symlinks=skipped_symlinks,
    )


def _read_and_verify_bundle(archive: zipfile.ZipFile) -> BundleManifest:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise FileSyncConfigError("sync bundle contains duplicate ZIP entries")
    if BUNDLE_MANIFEST_PATH not in names:
        raise FileSyncConfigError("sync bundle is missing manifest.json")
    manifest_info = archive.getinfo(BUNDLE_MANIFEST_PATH)
    if manifest_info.file_size > BUNDLE_MANIFEST_MAX_BYTES:
        raise FileSyncConfigError(
            f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
        )
    try:
        with archive.open(manifest_info, "r") as source:
            manifest_payload = source.read(BUNDLE_MANIFEST_MAX_BYTES + 1)
        if len(manifest_payload) > BUNDLE_MANIFEST_MAX_BYTES:
            raise FileSyncConfigError(
                f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
            )
        raw_manifest = json.loads(manifest_payload.decode("utf-8"))
    except (KeyError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise FileSyncConfigError(f"cannot read sync bundle manifest: {exc}") from exc
    manifest = _parse_bundle_manifest(raw_manifest)

    expected_names = {BUNDLE_MANIFEST_PATH}
    for entry in manifest.entries:
        if entry.kind is EntryKind.FILE:
            expected_names.add(_bundle_payload_path(entry.path))
    actual_names = set(names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise FileSyncConfigError(
            f"sync bundle payload does not match manifest: {'; '.join(detail)}"
        )

    info_by_name = {info.filename: info for info in infos}
    for entry in manifest.entries:
        if entry.kind is not EntryKind.FILE:
            continue
        payload_path = _bundle_payload_path(entry.path)
        info = info_by_name[payload_path]
        if info.is_dir() or info.file_size != entry.size:
            raise FileSyncConfigError(
                f"sync bundle size mismatch for {entry.path.as_posix()}"
            )
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(info, "r") as source:
                while True:
                    chunk = source.read(CONTENT_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > entry.size:
                        raise FileSyncConfigError(
                            f"sync bundle size mismatch for {entry.path.as_posix()}"
                        )
                    digest.update(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise FileSyncConfigError(
                f"cannot verify sync bundle payload {entry.path.as_posix()}: {exc}"
            ) from exc
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise FileSyncConfigError(
                f"sync bundle checksum mismatch for {entry.path.as_posix()}"
            )
    return manifest


def verify_sync_bundle(bundle_path: Path) -> BundleManifest:
    bundle = Path(os.path.abspath(bundle_path))
    if not bundle.is_file():
        raise FileSyncConfigError(f"sync bundle is not a file: {bundle}")
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            return _read_and_verify_bundle(archive)
    except FileSyncConfigError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileSyncConfigError(f"cannot read sync bundle {bundle}: {exc}") from exc


def _bundle_file_content_matches(
    archive: zipfile.ZipFile,
    entry: BundleEntry,
    target_path: Path,
) -> bool:
    try:
        with archive.open(_bundle_payload_path(entry.path), "r") as source:
            source_digest = _content_digest(source)
        with target_path.open("rb") as target:
            target_digest = _content_digest(target)
    except Exception as exc:
        raise FileSyncError(
            f"cannot compare bundle and target content for {entry.path.as_posix()}: "
            f"{exc}"
        ) from exc
    return _content_digests_equal(source_digest, target_digest)


def _apply_action_message(action: SyncAction, *, dry_run: bool) -> str:
    verbs = {
        ActionKind.COPY_TO_REMOTE: "write",
        ActionKind.CREATE_REMOTE_DIRECTORY: "create directory",
        ActionKind.DELETE_REMOTE: "delete",
    }
    prefix = "would " if dry_run else ""
    return f"{prefix}{verbs[action.kind]}: {action.path.as_posix()}"


def _require_safe_target_parent(target_root: Path, relative: PurePosixPath) -> None:
    current = target_root
    for part in relative.parent.parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise FileSyncError(
                f"bundle target parent does not exist: {current}"
            ) from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise FileSyncError(
                f"bundle target parent is not a safe directory: {current}"
            )


def _write_bundle_file(
    archive: zipfile.ZipFile,
    entry: BundleEntry,
    target_path: Path,
) -> None:
    try:
        current = target_path.lstat()
    except FileNotFoundError:
        current = None
    existing_mode = (
        stat.S_IMODE(current.st_mode)
        if current is not None and stat.S_ISREG(current.st_mode)
        else None
    )
    if current is not None and not stat.S_ISREG(current.st_mode):
        _remove_local_for_replace(target_path)

    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target_path.name}.hgc-", dir=target_path.parent
        )
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(file_descriptor, "wb") as destination:
            file_descriptor = -1
            with archive.open(_bundle_payload_path(entry.path), "r") as source:
                while True:
                    chunk = source.read(CONTENT_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    digest.update(chunk)
                    destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise FileSyncError(
                f"sync bundle changed while applying {entry.path.as_posix()}"
            )
        mode = existing_mode if existing_mode is not None else entry.mode
        if os.name != "nt":
            os.chmod(temporary_path, mode)
        os.utime(temporary_path, ns=(entry.mtime_ns, entry.mtime_ns))
        os.replace(temporary_path, target_path)
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _apply_bundle_action(
    archive: zipfile.ZipFile,
    action: SyncAction,
    entries: dict[PurePosixPath, BundleEntry],
    target_root: Path,
) -> None:
    target_path = target_root.joinpath(*action.path.parts)
    if action.path != PurePosixPath("."):
        _require_safe_target_parent(target_root, action.path)
    if action.kind is ActionKind.DELETE_REMOTE:
        _delete_local(target_path, action.existing)
        return
    if action.kind is ActionKind.CREATE_REMOTE_DIRECTORY:
        if action.path == PurePosixPath("."):
            target_root.mkdir(parents=True, exist_ok=True)
            return
        try:
            current = target_path.lstat()
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISDIR(current.st_mode):
            _remove_local_for_replace(target_path)
        target_path.mkdir(exist_ok=True)
        return
    if action.kind is ActionKind.COPY_TO_REMOTE:
        _write_bundle_file(archive, entries[action.path], target_path)
        return
    raise FileSyncError(f"unsupported bundle action: {action.kind.value}")


def _restore_created_directory_metadata(
    actions: Sequence[SyncAction],
    entries: dict[PurePosixPath, BundleEntry],
    target_root: Path,
) -> None:
    created = [
        action.path
        for action in actions
        if action.kind is ActionKind.CREATE_REMOTE_DIRECTORY
        and action.path != PurePosixPath(".")
        and action.path in entries
    ]
    for path in sorted(
        created, key=lambda value: (-len(value.parts), value.as_posix())
    ):
        entry = entries[path]
        target_path = target_root.joinpath(*path.parts)
        if os.name != "nt":
            os.chmod(target_path, entry.mode)
        os.utime(target_path, ns=(entry.mtime_ns, entry.mtime_ns))


def _first_ignored_descendant(
    target_root: Path,
    directory: PurePosixPath,
    ignore: IgnoreMatcher,
) -> PurePosixPath | None:
    base = target_root.joinpath(*directory.parts)

    def visit(
        local_directory: Path, relative_directory: PurePosixPath
    ) -> PurePosixPath | None:
        try:
            children = sorted(os.scandir(local_directory), key=lambda item: item.name)
        except OSError as exc:
            raise FileSyncError(
                f"cannot inspect target directory before replacement {local_directory}: "
                f"{exc}"
            ) from exc
        for child in children:
            relative = relative_directory / child.name
            try:
                is_directory = child.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise FileSyncError(
                    f"cannot inspect target path before replacement {child.path}: {exc}"
                ) from exc
            if ignore.matches(relative, directory=is_directory):
                return relative
            if is_directory:
                match = visit(Path(child.path), relative)
                if match is not None:
                    return match
        return None

    return visit(base, directory)


def apply_sync_bundle(
    bundle_path: Path,
    target_root: Path,
    *,
    delete: bool = False,
    skip_create: bool = False,
    ignore_existing: bool = False,
    update: bool = False,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> ApplyReport:
    emit = progress or (lambda _message: None)
    bundle = Path(os.path.abspath(bundle_path))
    target = Path(os.path.abspath(target_root))
    if not bundle.is_file():
        raise FileSyncConfigError(f"sync bundle is not a file: {bundle}")
    if os.path.lexists(target) and (target.is_symlink() or not target.is_dir()):
        raise FileSyncConfigError(f"bundle target is not a directory: {target}")

    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            manifest = _read_and_verify_bundle(archive)
            entries = {entry.path: entry for entry in manifest.entries}
            protected_paths = [
                PurePosixPath(CONFIG_RELATIVE_PATH.as_posix()),
                *manifest.skipped_symlinks,
            ]
            try:
                bundle_relative = bundle.relative_to(target)
            except ValueError:
                bundle_relative = None
            else:
                if bundle_relative.parts:
                    bundle_relative_path = PurePosixPath(bundle_relative.as_posix())
                    destructive_entry = next(
                        (
                            entry
                            for entry in manifest.entries
                            if entry.path == bundle_relative_path
                            or (
                                entry.path in bundle_relative_path.parents
                                and entry.kind is not EntryKind.DIRECTORY
                            )
                        ),
                        None,
                    )
                    destructive_deletion = next(
                        (
                            path
                            for path in manifest.deletions
                            if path == bundle_relative_path
                            or path in bundle_relative_path.parents
                        ),
                        None,
                    )
                    if (
                        destructive_entry is not None
                        or destructive_deletion is not None
                    ):
                        raise FileSyncConfigError(
                            "sync bundle is inside the target at a managed path"
                        )
                    protected_paths.append(bundle_relative_path)

            patterns = (
                *manifest.ignore_patterns,
                ".git/",
                PROTECTED_CONFIG_PATTERN,
            )
            ignore = IgnoreMatcher(patterns, tuple(dict.fromkeys(protected_paths)))
            selected_paths = (
                frozenset({*entries, *manifest.deletions})
                if manifest.mode is BundleMode.GIT_PATCH
                else None
            )
            target_snapshot = scan_local(target, ignore, paths=selected_paths)
            source_snapshot = Snapshot(
                exists=True,
                entries={
                    path: entry.as_file_entry() for path, entry in entries.items()
                },
            )
            options = SyncOptions(
                delete=delete,
                skip_create=skip_create,
                ignore_existing=ignore_existing,
                update=update,
            )
            actions = build_sync_plan(
                SyncDirection.LOCAL_TO_REMOTE,
                source_snapshot,
                target_snapshot,
                options,
            )
            if selected_paths is not None:
                actions = _filter_actions_for_git_paths(actions, selected_paths)

            for action in actions:
                if (
                    action.kind is ActionKind.COPY_TO_REMOTE
                    and action.existing is not None
                    and action.existing.kind is EntryKind.DIRECTORY
                ):
                    ignored_descendant = _first_ignored_descendant(
                        target, action.path, ignore
                    )
                    if ignored_descendant is not None:
                        raise FileSyncError(
                            "cannot replace target directory containing an ignored "
                            f"path: {ignored_descendant.as_posix()}"
                        )

            filtered_actions: list[SyncAction] = []
            for action in actions:
                entry = entries.get(action.path)
                if (
                    action.kind is ActionKind.COPY_TO_REMOTE
                    and entry is not None
                    and entry.kind is EntryKind.FILE
                    and action.existing is not None
                    and action.existing.kind is EntryKind.FILE
                    and _content_sizes_may_match(entry.size, action.existing.size)
                    and _bundle_file_content_matches(
                        archive,
                        entry,
                        target.joinpath(*action.path.parts),
                    )
                ):
                    continue
                filtered_actions.append(action)
            non_deletes = [
                action
                for action in filtered_actions
                if action.kind is not ActionKind.DELETE_REMOTE
            ]
            deletes = [
                action
                for action in filtered_actions
                if action.kind is ActionKind.DELETE_REMOTE
            ]
            actions = [*non_deletes, *deletes]

            emit(f"verified bundle: {bundle}")
            emit(f"target: {target}")
            if dry_run:
                for action in actions:
                    emit(_apply_action_message(action, dry_run=True))
            else:
                for action in actions:
                    emit(_apply_action_message(action, dry_run=False))
                    _apply_bundle_action(archive, action, entries, target)
                _restore_created_directory_metadata(actions, entries, target)
    except (FileSyncConfigError, FileSyncError):
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileSyncError(f"cannot apply sync bundle {bundle}: {exc}") from exc

    emit(f"bundle {'plan' if dry_run else 'applied'}: {len(actions)} action(s)")
    return ApplyReport(
        bundle_path=bundle,
        target_root=target,
        manifest=manifest,
        actions=tuple(actions),
        dry_run=dry_run,
    )


class SFTPRemote:
    def __init__(self, config: SFTPConfig) -> None:
        self.config = config
        self._client = None
        self._sftp = None
        self._proxy = None
        self._confirmed_directories: set[str] = set()

    def __enter__(self) -> Self:
        self._confirmed_directories.clear()
        previous_agent_socket: str | None = None
        restore_agent_socket = False
        try:
            import paramiko

            host = self.config.host
            port = self.config.port
            username = self.config.username
            key_filename: str | list[str] | None = (
                str(self.config.private_key_path)
                if self.config.private_key_path is not None
                else None
            )
            proxy_command = None
            if self.config.ssh_config_path.is_file():
                ssh_config = paramiko.SSHConfig()
                explicit_ssh_values = ["Host *"]
                if self.config.config_path is None:
                    if port is not None:
                        explicit_ssh_values.append(f"  Port {port}")
                    if username is not None:
                        explicit_ssh_values.append(f"  User {username}")
                if len(explicit_ssh_values) > 1:
                    ssh_config.parse(io.StringIO("\n".join(explicit_ssh_values)))
                with self.config.ssh_config_path.open(encoding="utf-8") as handle:
                    ssh_config.parse(handle)
                resolved = ssh_config.lookup(host)
                host = resolved.get("hostname", host)
                port = port or int(resolved.get("port", 22))
                username = username or resolved.get("user")
                key_filename = key_filename or resolved.get("identityfile")
                proxy_command = resolved.get("proxycommand")
            port = port or 22
            username = username or getpass.getuser()

            if self.config.agent:
                agent = self.config.agent
                if agent.startswith("$"):
                    variable = agent[1:]
                    agent = os.environ.get(variable, "")
                    if not agent:
                        raise FileSyncConfigError(
                            f"environment variable {variable!r} referenced by agent "
                            "is not set"
                        )
                previous_agent_socket = os.environ.get("SSH_AUTH_SOCK")
                os.environ["SSH_AUTH_SOCK"] = agent
                restore_agent_socket = True

            passphrase = self.config.passphrase
            if passphrase is True:
                passphrase = getpass.getpass(
                    f"[{self.config.host}] private key passphrase: "
                )
            if proxy_command and proxy_command.lower() != "none":
                self._proxy = paramiko.ProxyCommand(proxy_command)

            client = paramiko.SSHClient()
            client.load_system_host_keys()
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=self.config.password,
                key_filename=key_filename,
                passphrase=passphrase if isinstance(passphrase, str) else None,
                timeout=self.config.connect_timeout,
                banner_timeout=self.config.connect_timeout,
                auth_timeout=max(self.config.connect_timeout, 60.0)
                if self.config.passphrase is True
                else self.config.connect_timeout,
                allow_agent=True,
                look_for_keys=True,
                sock=self._proxy,
            )
            self._client = client
            self._sftp = client.open_sftp()
            return self
        except FileSyncConfigError:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            raise FileSyncError(
                f"SFTP connection failed for {self.config.endpoint}: {exc}"
            ) from exc
        finally:
            if restore_agent_socket:
                if previous_agent_socket is None:
                    os.environ.pop("SSH_AUTH_SOCK", None)
                else:
                    os.environ["SSH_AUTH_SOCK"] = previous_agent_socket

    def __exit__(self, _type, _value, _traceback) -> None:
        self._confirmed_directories.clear()
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._sftp = None
            try:
                if self._client is not None:
                    self._client.close()
            finally:
                self._client = None
                if self._proxy is not None:
                    self._proxy.close()
                self._proxy = None

    @property
    def sftp(self):
        if self._sftp is None:
            raise FileSyncError("SFTP session is not connected")
        return self._sftp

    def _path(self, relative: PurePosixPath | None = None) -> str:
        if relative is None or not relative.parts:
            return self.config.remote_root
        return posixpath.join(self.config.remote_root, relative.as_posix())

    @staticmethod
    def _missing(error: BaseException) -> bool:
        return (
            isinstance(error, FileNotFoundError) or getattr(error, "errno", None) == 2
        )

    @staticmethod
    def _entry_kind(mode: int) -> EntryKind | None:
        if stat.S_ISDIR(mode):
            return EntryKind.DIRECTORY
        if stat.S_ISLNK(mode):
            return EntryKind.SYMLINK
        if stat.S_ISREG(mode):
            return EntryKind.FILE
        return None

    @staticmethod
    def _safe_child_name(parent: str, value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\0" in value
        ):
            raise FileSyncError(
                f"unsafe remote directory entry {value!r} under {parent}"
            )
        return value

    def snapshot(
        self,
        ignore: IgnoreMatcher,
        *,
        paths: frozenset[PurePosixPath] | None = None,
    ) -> Snapshot:
        self._confirmed_directories.clear()
        try:
            root_info = self.sftp.lstat(self.config.remote_root)
        except OSError as exc:
            if self._missing(exc):
                return Snapshot(False, {})
            raise FileSyncError(
                f"cannot stat remote path {self.config.remote_root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode):
            raise FileSyncError(
                f"remote path is not a directory: {self.config.remote_root}"
            )

        entries: dict[PurePosixPath, FileEntry] = {}
        selected_paths = _scan_path_selection(paths)
        if selected_paths == frozenset():
            return Snapshot(True, entries)

        pending: list[tuple[str, PurePosixPath | None]] = [
            (self.config.remote_root, None)
        ]
        while pending:
            remote_dir, relative_dir = pending.pop()
            try:
                children = sorted(
                    self.sftp.listdir_attr(remote_dir), key=lambda item: item.filename
                )
            except OSError as exc:
                raise FileSyncError(
                    f"cannot list remote directory {remote_dir}: {exc}"
                ) from exc
            for child in children:
                child_name = self._safe_child_name(remote_dir, child.filename)
                relative = (
                    PurePosixPath(child_name)
                    if relative_dir is None
                    else relative_dir / child_name
                )
                if selected_paths is not None and relative not in selected_paths:
                    continue
                kind = self._entry_kind(child.st_mode)
                if kind is None:
                    continue
                if ignore.matches(relative, directory=kind is EntryKind.DIRECTORY):
                    continue
                remote_path = posixpath.join(remote_dir, child_name)
                try:
                    link_target = (
                        self.sftp.readlink(remote_path)
                        if kind is EntryKind.SYMLINK
                        else None
                    )
                except OSError as exc:
                    raise FileSyncError(
                        f"cannot read remote symlink {remote_path}: {exc}"
                    ) from exc
                entries[relative] = FileEntry(
                    kind=kind,
                    size=child.st_size,
                    mtime=child.st_mtime - self.config.remote_time_offset,
                    mode=stat.S_IMODE(child.st_mode),
                    link_target=link_target,
                )
                if kind is EntryKind.DIRECTORY:
                    pending.append((remote_path, relative))

        return Snapshot(True, entries)

    def equivalent_file_content(
        self, relative: PurePosixPath, local_root: Path
    ) -> bool:
        local_path = local_root.joinpath(*relative.parts)
        remote_path = self._path(relative)
        try:
            with local_path.open("rb") as local_handle:
                local_digest = _content_digest(local_handle)
            with self.sftp.open(remote_path, "rb") as remote_handle:
                remote_digest = _content_digest(remote_handle)
        except Exception as exc:
            raise FileSyncError(
                f"cannot compare local and remote content for {relative.as_posix()}: "
                f"{exc}"
            ) from exc
        return _content_digests_equal(local_digest, remote_digest)

    def _lstat(self, path: str):
        try:
            return self.sftp.lstat(path)
        except OSError as exc:
            if self._missing(exc):
                return None
            raise

    @staticmethod
    def _is_remote_root(path: str) -> bool:
        return path in {"", ".", "/"} or bool(re.fullmatch(r"[A-Za-z]:/?", path))

    def _ensure_directory(self, path: str, mode: int | None = None) -> None:
        if path in self._confirmed_directories:
            return
        info = self._lstat(path)
        if info is not None:
            if stat.S_ISDIR(info.st_mode):
                self._confirmed_directories.add(path)
                return
            self._remove_tree(path, info.st_mode)
        if self._is_remote_root(path):
            return
        parent = posixpath.dirname(path)
        if parent != path:
            self._ensure_directory(parent, mode)
        if mode is None:
            self.sftp.mkdir(path)
        else:
            self.sftp.mkdir(path, mode=mode)
        if mode is not None:
            self.sftp.chmod(path, mode)
        self._confirmed_directories.add(path)

    def ensure_root(self) -> None:
        self._ensure_directory(self.config.remote_root, self.config.dir_perm)

    def _remove_tree(self, path: str, mode: int | None = None) -> None:
        self._confirmed_directories.discard(path)
        info = self._lstat(path) if mode is None else None
        actual_mode = info.st_mode if info is not None else mode
        if actual_mode is None:
            return
        if stat.S_ISDIR(actual_mode):
            # Replacing a directory invalidates every cached descendant too.
            self._confirmed_directories.clear()
            for child in self.sftp.listdir_attr(path):
                child_name = self._safe_child_name(path, child.filename)
                child_path = posixpath.join(path, child_name)
                self._remove_tree(child_path, child.st_mode)
            self.sftp.rmdir(path)
        else:
            self.sftp.remove(path)

    def _delete(self, path: str, entry: FileEntry | None) -> None:
        self._confirmed_directories.discard(path)
        if entry is not None and entry.kind is EntryKind.DIRECTORY:
            self._confirmed_directories.clear()
        try:
            if entry is not None and entry.kind is EntryKind.DIRECTORY:
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
        except OSError as exc:
            if self._missing(exc):
                return
            if entry is not None and entry.kind is EntryKind.DIRECTORY:
                try:
                    if self.sftp.listdir(path):
                        return
                except OSError:
                    pass
            raise

    def _prepare_destination(self, path: str, desired: EntryKind) -> None:
        info = self._lstat(path)
        if info is None:
            self._confirmed_directories.discard(path)
            return
        current = self._entry_kind(info.st_mode)
        if current is desired and desired in {EntryKind.FILE, EntryKind.DIRECTORY}:
            return
        self._remove_tree(path, info.st_mode)

    def _temporary_upload_path(self, remote_path: str) -> str:
        parent = posixpath.dirname(remote_path)
        for _attempt in range(16):
            candidate = posixpath.join(
                parent, f".hgc-upload-{secrets.token_hex(16)}.tmp"
            )
            if self._lstat(candidate) is None:
                return candidate
        raise FileSyncError(
            f"cannot allocate a unique remote upload path beside {remote_path}"
        )

    def _upload_file(
        self, local_path: Path, remote_path: str, action: SyncAction
    ) -> None:
        self._ensure_directory(posixpath.dirname(remote_path), self.config.dir_perm)
        self._prepare_destination(remote_path, EntryKind.FILE)
        upload_path = (
            self._temporary_upload_path(remote_path)
            if self.config.use_temp_file
            else remote_path
        )
        try:
            self.sftp.put(str(local_path), upload_path, confirm=True)
            source = action.source
            if source is not None:
                remote_time = source.mtime + self.config.remote_time_offset
                self.sftp.utime(upload_path, (remote_time, remote_time))
                mode = self.config.file_perm
                if mode is None and action.existing is not None:
                    mode = action.existing.mode
                if mode is None:
                    mode = source.mode
                if mode is not None:
                    self.sftp.chmod(upload_path, mode)
            if upload_path != remote_path:
                if self.config.open_ssh:
                    self.sftp.posix_rename(upload_path, remote_path)
                else:
                    self._remove_tree(remote_path)
                    self.sftp.rename(upload_path, remote_path)
        except Exception:
            if upload_path != remote_path:
                try:
                    self._remove_tree(upload_path)
                except Exception:
                    pass
            raise

    def _upload_symlink(
        self, local_path: Path, remote_path: str, action: SyncAction
    ) -> None:
        self._ensure_directory(posixpath.dirname(remote_path), self.config.dir_perm)
        self._prepare_destination(remote_path, EntryKind.SYMLINK)
        target = action.source.link_target if action.source else os.readlink(local_path)
        if target is None:
            raise FileSyncError(f"cannot read local symlink target: {local_path}")
        self.sftp.symlink(target, remote_path)

    def _download_file(
        self, remote_path: str, local_path: Path, action: SyncAction
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = local_path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            _remove_local_for_replace(local_path)
            info = None
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{local_path.name}.hgc-", dir=local_path.parent
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            self.sftp.get(remote_path, str(temp_path))
            source = action.source
            if source is not None:
                os.utime(temp_path, (source.mtime, source.mtime))
                mode = stat.S_IMODE(info.st_mode) if info is not None else source.mode
                if mode is not None:
                    os.chmod(temp_path, mode)
            os.replace(temp_path, local_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _download_symlink(
        self, remote_path: str, local_path: Path, action: SyncAction
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _remove_local_for_replace(local_path)
        target = (
            action.source.link_target
            if action.source
            else self.sftp.readlink(remote_path)
        )
        if target is None:
            raise FileSyncError(f"cannot read remote symlink target: {remote_path}")
        os.symlink(target, local_path)

    def apply(self, action: SyncAction, local_root: Path) -> None:
        local_path = local_root.joinpath(*action.path.parts)
        remote_path = self._path(action.path)
        if action.kind is ActionKind.DELETE_REMOTE:
            self._delete(remote_path, action.existing)
        elif action.kind is ActionKind.DELETE_LOCAL:
            _delete_local(local_path, action.existing)
        elif action.kind is ActionKind.CREATE_REMOTE_DIRECTORY:
            self._prepare_destination(remote_path, EntryKind.DIRECTORY)
            self._ensure_directory(remote_path, self.config.dir_perm)
        elif action.kind is ActionKind.CREATE_LOCAL_DIRECTORY:
            try:
                is_directory = local_path.is_dir() and not local_path.is_symlink()
            except OSError:
                is_directory = False
            if not is_directory and (local_path.exists() or local_path.is_symlink()):
                _remove_local_for_replace(local_path)
            local_path.mkdir(parents=True, exist_ok=True)
        elif action.kind is ActionKind.COPY_TO_REMOTE:
            if action.source is not None and action.source.kind is EntryKind.SYMLINK:
                self._upload_symlink(local_path, remote_path, action)
            else:
                self._upload_file(local_path, remote_path, action)
        elif action.kind is ActionKind.COPY_TO_LOCAL:
            if action.source is not None and action.source.kind is EntryKind.SYMLINK:
                self._download_symlink(remote_path, local_path, action)
            else:
                self._download_file(remote_path, local_path, action)


def sync_workspace_files(
    workspace_root: Path,
    direction: SyncDirection,
    *,
    profile: str | None = None,
    remote_endpoint: str | None = None,
    port: int | None = None,
    identity: Path | None = None,
    exclude: Sequence[str] = (),
    delete: bool = False,
    skip_create: bool = False,
    ignore_existing: bool = False,
    update: bool = False,
    git_changed: bool = False,
    dry_run: bool = False,
    progress: Progress | None = None,
    remote_factory: RemoteFactory = SFTPRemote,
) -> SyncReport:
    emit = progress or (lambda _message: None)
    if git_changed and direction is not SyncDirection.LOCAL_TO_REMOTE:
        raise FileSyncUsageError(
            "--git-changed is only supported for local-to-remote sync"
        )
    temporary_options_used = [
        option
        for option, used in (
            ("--port", port is not None),
            ("--identity", identity is not None),
            ("--exclude", bool(exclude)),
            ("--delete", delete),
            ("--skip-create", skip_create),
            ("--ignore-existing", ignore_existing),
            ("--update", update),
        )
        if used
    ]
    if remote_endpoint is None:
        if temporary_options_used:
            raise FileSyncUsageError(
                f"temporary-only options {', '.join(temporary_options_used)} "
                "require a remote endpoint (REMOTE). In config mode, set the "
                "corresponding fields in .vscode/sftp.json instead."
            )
        config = load_sftp_config(workspace_root, profile)
    else:
        if profile is not None:
            raise FileSyncUsageError(
                "remote endpoint and --profile are mutually exclusive"
            )
        if direction is SyncDirection.BOTH and (delete or update):
            raise FileSyncUsageError(
                "--delete and --update are not supported for bidirectional sync"
            )
        config = build_temporary_sftp_config(
            workspace_root,
            remote_endpoint,
            port=port,
            identity=identity,
            exclude=exclude,
            sync_options=SyncOptions(
                delete=delete,
                skip_create=skip_create,
                ignore_existing=ignore_existing,
                update=update,
            ),
        )
    ignore = IgnoreMatcher(config.ignore_patterns, config.protected_paths)
    emit(f"config: {config.source}")
    if config.config_path is not None:
        emit(f"profile: {config.selection}")
    emit(f"mapping: {config.local_root} <-> {config.endpoint}")

    changed_paths: frozenset[PurePosixPath] | None = None
    if git_changed:
        emit("detecting Git changes...")
        changed_paths = git_changed_paths(config.local_root)
        emit(f"Git changes: {len(changed_paths)} path(s)")
        if not changed_paths:
            return SyncReport(
                config=config,
                direction=direction,
                actions=(),
                dry_run=dry_run,
            )

    emit("scanning local and remote files...")

    local = scan_local(config.local_root, ignore, paths=changed_paths)
    try:
        with remote_factory(config) as remote:
            remote_snapshot = (
                remote.snapshot(ignore, paths=changed_paths)
                if changed_paths is not None
                else remote.snapshot(ignore)
            )
            actions = build_sync_plan(
                direction, local, remote_snapshot, config.sync_options
            )
            if changed_paths is not None:
                actions = _filter_actions_for_git_paths(actions, changed_paths)
            if any(
                action.kind in {ActionKind.COPY_TO_LOCAL, ActionKind.COPY_TO_REMOTE}
                and action.source is not None
                and action.source.kind is EntryKind.FILE
                and action.existing is not None
                and action.existing.kind is EntryKind.FILE
                for action in actions
            ):
                emit("comparing changed file contents...")
                actions = _remove_equivalent_file_actions(
                    actions, remote, config.local_root
                )
            if not dry_run:
                # Deletes operate on already-scanned roots; only copy/create actions
                # require creating a missing destination root.
                writes_local = any(
                    action.kind
                    in {
                        ActionKind.COPY_TO_LOCAL,
                        ActionKind.CREATE_LOCAL_DIRECTORY,
                    }
                    for action in actions
                )
                writes_remote = any(
                    action.kind
                    in {
                        ActionKind.COPY_TO_REMOTE,
                        ActionKind.CREATE_REMOTE_DIRECTORY,
                    }
                    for action in actions
                )
                if writes_local:
                    config.local_root.mkdir(parents=True, exist_ok=True)
                if writes_remote:
                    remote.ensure_root()
                for action in actions:
                    emit(format_action(action, dry_run=False))
                    remote.apply(action, config.local_root)
            else:
                for action in actions:
                    emit(format_action(action, dry_run=True))
    except (FileSyncConfigError, FileSyncError):
        raise
    except Exception as exc:
        raise FileSyncError(f"file sync failed after partial progress: {exc}") from exc

    return SyncReport(
        config=config,
        direction=direction,
        actions=tuple(actions),
        dry_run=dry_run,
    )
