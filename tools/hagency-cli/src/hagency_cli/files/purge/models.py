from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

MIN_AGE_SECONDS = 7 * 24 * 60 * 60


MIN_SCAN_DEPTH = 1


MAX_SCAN_DEPTH = 6


CACHEDIR_TAG_NAME = "CACHEDIR.TAG"


CACHEDIR_TAG_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"


PURGE_TARGETS = frozenset(
    {
        "node_modules",
        "target",
        "build",
        "dist",
        "venv",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".ruff_cache",
        ".gradle",
        ".terragrunt-cache",
        "__pycache__",
        ".next",
        ".nuxt",
        ".output",
        "vendor",
        "bin",
        "obj",
        ".turbo",
        ".parcel-cache",
        ".dart_tool",
        ".zig-cache",
        "zig-out",
        ".angular",
        ".svelte-kit",
        ".astro",
        "coverage",
        "DerivedData",
        "Pods",
        ".cxx",
        ".expo",
        ".build",
    }
)


MONOREPO_INDICATORS = (
    ".git",
    "lerna.json",
    "pnpm-workspace.yaml",
    "nx.json",
    "rush.json",
)


PROJECT_INDICATORS = (
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "terragrunt.hcl",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Makefile",
    "build.zig",
    "build.zig.zon",
    ".git",
)


DEFAULT_ROOT_NAMES = (
    "www",
    "dev",
    "Projects",
    "GitHub",
    "Code",
    "Workspace",
    "Repos",
    "Development",
)


EXPLICIT_HIDDEN_ROOTS = (
    Path(".codex") / "worktrees",
    Path(".claude") / "worktrees",
)


EXCLUDED_HOME_CHILDREN = frozenset(
    {
        "Applications",
        "AppData",
        "Box",
        "Desktop",
        "Documents",
        "Downloads",
        "Library",
        "Movies",
        "Music",
        "Pictures",
        "Public",
        "Dropbox",
        "Google Drive",
        "iCloud Drive",
    }
)


CLOUD_HOME_CHILD_PREFIXES = (
    "box",
    "creative cloud files",
    "dropbox",
    "google drive",
    "icloud",
    "nextcloud",
    "onedrive",
    "owncloud",
)


SCAN_PRUNE_NAMES = frozenset(
    {".git", ".hg", ".svn", ".Trash", "Applications", "AppData", "Library"}
)


SCAN_PRUNE_NAMES_CASEFOLD = frozenset(name.casefold() for name in SCAN_PRUNE_NAMES)


class Activity(str, Enum):
    OLD = "old"
    RECENT = "recent"
    UNCERTAIN = "uncertain"


class PurgeDisposition(str, Enum):
    PREVIEW = "preview"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    PARTIAL = "partial"


class ItemDisposition(str, Enum):
    WOULD_REMOVE = "would_remove"
    REMOVED = "removed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class PurgeRequest:
    paths: tuple[Path, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class PurgeChoice:
    id: str
    exact_path: Path
    project_path: Path
    artifact_kind: str
    size_bytes: int | None
    activity: Activity
    preselected: bool

    @property
    def label(self) -> str:
        size = (
            _format_bytes(self.size_bytes) if self.size_bytes is not None else "unknown"
        )
        return (
            f"{self.project_path.name} | {self.artifact_kind} | {size} | "
            f"{self.activity.value} | {self.exact_path}"
        )


class PurgeUI(Protocol):
    def is_interactive(self) -> bool: ...

    def select(self, choices: tuple[PurgeChoice, ...]) -> tuple[str, ...] | None: ...

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool: ...


@dataclass(frozen=True)
class PurgeIssue:
    code: str
    path: Path | None
    message: str
    is_failure: bool = True


@dataclass(frozen=True)
class PurgeItemResult:
    exact_path: Path
    disposition: ItemDisposition
    size_bytes: int | None
    message: str = ""


@dataclass(frozen=True)
class PurgeReport:
    disposition: PurgeDisposition
    roots: tuple[Path, ...]
    choices: tuple[PurgeChoice, ...]
    selected_paths: tuple[Path, ...]
    results: tuple[PurgeItemResult, ...]
    issues: tuple[PurgeIssue, ...]
    known_bytes: int = 0

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues) or any(
            result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
            for result in self.results
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class PathsEditReport:
    config_path: Path
    before_roots: tuple[Path, ...]
    after_roots: tuple[Path, ...]
    editor: str
    issues: tuple[PurgeIssue, ...]

    @property
    def failed(self) -> bool:
        return any(issue.is_failure for issue in self.issues)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


@dataclass(frozen=True)
class _Identity:
    device: int
    inode: int
    mode: int


@dataclass(frozen=True)
class _GitContext:
    root: Path
    marker_identity: _Identity


@dataclass(frozen=True)
class _HardlinkEntry:
    identity: tuple[int, int] | None
    size_bytes: int


@dataclass(frozen=True)
class _PlannedCandidate:
    choice: PurgeChoice
    root: Path
    root_identity: _Identity
    parent_identity: _Identity
    target_identity: _Identity
    git_contexts: tuple[_GitContext, ...]
    hardlink_entries: tuple[_HardlinkEntry, ...]


@dataclass(frozen=True)
class _PurgePlan:
    roots: tuple[Path, ...]
    candidates: tuple[_PlannedCandidate, ...]
    issues: tuple[PurgeIssue, ...]


@dataclass(frozen=True)
class _ConfiguredPaths:
    paths: tuple[Path, ...]
    has_entries: bool
    issues: tuple[PurgeIssue, ...]


@dataclass(frozen=True)
class _DiscoveredRoots:
    paths: tuple[Path, ...]
    issues: tuple[PurgeIssue, ...]


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")
