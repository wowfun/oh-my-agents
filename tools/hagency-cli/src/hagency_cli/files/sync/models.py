from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from hagency_cli.files.sync.selection import IgnoreMatcher


CONFIG_RELATIVE_PATH = Path(".vscode") / "sftp.json"


PROTECTED_CONFIG_PATTERN = "**/.vscode/sftp.json"


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


def _require_mapping(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise FileSyncConfigError(f"{label} must be a JSON object")
    return value
