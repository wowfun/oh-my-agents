from __future__ import annotations

from enum import Enum, auto
from pathlib import PurePosixPath

from hagency_cli.files.sync.models import (
    ActionKind,
    EntryKind,
    FileEntry,
    FileSyncError,
    Snapshot,
    SyncAction,
    SyncDirection,
    SyncOptions,
)


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


class _ForcedSide(Enum):
    UNFORCED = auto()
    LOCAL = auto()
    REMOTE = auto()
    SKIP = auto()


def _forced_side(
    path: PurePosixPath, forced: dict[PurePosixPath, _ForcedSide]
) -> _ForcedSide:
    for parent in path.parents:
        if parent in forced:
            return forced[parent]
    return _ForcedSide.UNFORCED


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

    forced: dict[PurePosixPath, _ForcedSide] = {}
    paths = sorted(
        local.entries.keys() | remote.entries.keys(),
        key=lambda path: (len(path.parts), path.as_posix()),
    )

    for path in paths:
        forced_source = _forced_side(path, forced)
        if forced_source is not _ForcedSide.UNFORCED:
            if forced_source is _ForcedSide.SKIP:
                continue
            source_is_local = forced_source is _ForcedSide.LOCAL
            source = (
                local.entries.get(path) if source_is_local else remote.entries.get(path)
            )
            if source is not None:
                actions.append(
                    _copy_action(path, source, None, source_is_local=source_is_local)
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
                    forced[path] = _ForcedSide.SKIP
                    continue
                source_is_local = local_entry.mtime >= remote_entry.mtime
                source = local_entry if source_is_local else remote_entry
                target = remote_entry if source_is_local else local_entry
                actions.append(
                    _copy_action(path, source, target, source_is_local=source_is_local)
                )
                forced[path] = (
                    _ForcedSide.LOCAL if source_is_local else _ForcedSide.REMOTE
                )
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
                forced[path] = _ForcedSide.SKIP
                continue
            actions.append(
                _copy_action(
                    path, source_entry, target_entry, source_is_local=source_is_local
                )
            )
            forced[path] = _ForcedSide.LOCAL if source_is_local else _ForcedSide.REMOTE
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
