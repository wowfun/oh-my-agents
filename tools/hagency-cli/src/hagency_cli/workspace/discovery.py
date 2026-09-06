from __future__ import annotations

from pathlib import Path

from hagency_cli.paths import expand_path
from hagency_cli.workspace.config import render_toml, write_toml
from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress, emit_event

HAGENCY_CONFIG_NAME = "hagency-config.toml"


def workspace_config_path(root: Path) -> Path:
    return root / HAGENCY_CONFIG_NAME


def _find_workspace_root(start: Path) -> Path | None:
    resolved = start.resolve()
    for path in (resolved, *resolved.parents):
        if workspace_config_path(path).exists():
            return path
    return None


def _installed_workspace_root() -> Path | None:
    module = Path(__file__).resolve()
    source_path = Path("tools/hagency-cli/src/hagency_cli/workspace/discovery.py")
    if module.parts[-len(source_path.parts) :] != source_path.parts:
        return None
    root = module.parents[len(source_path.parts) - 1]
    # Trust only this source checkout, never ancestors of a tool environment.
    return root if workspace_config_path(root).is_file() else None


def resolve_workspace_root(value: str | None, cwd: Path) -> Path:
    if value:
        root = expand_path(value, cwd).resolve()
        config = workspace_config_path(root)
        if not config.exists():
            fail(f"missing workspace config: {config}")
        return root

    start = cwd.resolve()
    current_root = _find_workspace_root(start)
    if current_root is not None:
        return current_root

    installed_root = _installed_workspace_root()
    if installed_root is not None:
        return installed_root

    fail(f"not a hagency workspace: {start}")


def init_workspace(
    value: str | None,
    cwd: Path,
    *,
    force: bool,
    dry_run: bool,
    progress: Progress | None = None,
) -> Path:
    root = expand_path(value, cwd).resolve() if value else cwd.resolve()
    config = workspace_config_path(root)
    data = {"defaults": {"checkout_dir": "~/Projects/references", "depth": 1}}

    if config.exists() and not force:
        fail(f"workspace config already exists: {config}")

    if dry_run:
        action = "overwrite" if config.exists() else "create"
        emit_event(progress, f"Would {action} workspace config: {config}")
        emit_event(progress, render_toml(data).rstrip())
        return root

    root.mkdir(parents=True, exist_ok=True)
    write_toml(config, data)
    emit_event(progress, f"initialized hagency workspace: {root}")
    return root
