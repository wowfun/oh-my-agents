from __future__ import annotations

from pathlib import Path

from hagency_cli.workspace.config import read_toml
from hagency_cli.workspace.discovery import (
    resolve_workspace_root,
    workspace_config_path,
)
from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.sources import resolve_sources


def workspace_root_arg(value: str | None) -> Path:
    return resolve_workspace_root(value, Path.cwd())


def load_registry(root: Path) -> dict:
    return read_toml(workspace_config_path(root))


def load_sources(root: Path, checkout_dir: str | None) -> dict:
    registry = load_registry(root)
    return resolve_sources(registry, repo_root=root, checkout_override=checkout_dir)


def default_sync_depth(registry: dict) -> int | None:
    depth = registry.get("defaults", {}).get("depth")
    if depth is None:
        return None
    if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
        fail("defaults.depth must be a positive integer")
    return depth
