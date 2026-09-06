from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from hagency_cli.files.sync.config import build_temporary_sftp_config, load_sftp_config
from hagency_cli.files.sync.content import _remove_equivalent_file_actions
from hagency_cli.files.sync.models import (
    ActionKind,
    EntryKind,
    FileSyncConfigError,
    FileSyncError,
    FileSyncUsageError,
    Progress,
    RemoteFactory,
    SyncDirection,
    SyncOptions,
    SyncReport,
)
from hagency_cli.files.sync.planning import build_sync_plan, format_action
from hagency_cli.files.sync.selection import (
    IgnoreMatcher,
    _filter_actions_for_git_paths,
    git_changed_paths,
    scan_local,
)
from hagency_cli.files.sync.sftp import SFTPRemote


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
        raise FileSyncUsageError("--git-changed is only supported for uploads")
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
    ignore = IgnoreMatcher(
        config.ignore_patterns,
        config.protected_paths,
        protect_git=True,
    )
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
