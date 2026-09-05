from __future__ import annotations

from pathlib import Path

from .common import die, expand_path, render_toml, write_toml

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
    source_path = Path("tools/hagency-cli/src/hagency_cli/workspace.py")
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
            die(f"missing workspace config: {config}")
        return root

    start = cwd.resolve()
    current_root = _find_workspace_root(start)
    if current_root is not None:
        return current_root

    installed_root = _installed_workspace_root()
    if installed_root is not None:
        return installed_root

    die(f"not a hagency workspace: {start}")


def init_workspace(value: str | None, cwd: Path, *, force: bool, dry_run: bool) -> None:
    root = expand_path(value, cwd).resolve() if value else cwd.resolve()
    config = workspace_config_path(root)
    data = {"defaults": {"checkout_dir": "~/Projects/references", "depth": 1}}

    if config.exists() and not force:
        die(f"workspace config already exists: {config}")

    if dry_run:
        action = "overwrite" if config.exists() else "create"
        print(f"Would {action} workspace config: {config}")
        print(render_toml(data).rstrip())
        return

    root.mkdir(parents=True, exist_ok=True)
    write_toml(config, data)
    print(f"initialized hagency workspace: {root}")
