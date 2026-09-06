from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from hagency_cli.files.sync.bundle.format import (
    _bundle_payload_path,
    _read_and_verify_bundle,
    _validate_bundle_source,
)
from hagency_cli.files.sync.content import (
    _content_digest,
    _content_digests_equal,
    _content_sizes_may_match,
)
from hagency_cli.files.sync.local import _delete_local, _remove_local_for_replace
from hagency_cli.files.sync.models import (
    CONFIG_RELATIVE_PATH,
    CONTENT_CHUNK_SIZE,
    PROTECTED_CONFIG_PATTERN,
    ActionKind,
    ApplyReport,
    BundleEntry,
    BundleMode,
    EntryKind,
    FileSyncConfigError,
    FileSyncError,
    Progress,
    Snapshot,
    SyncAction,
    SyncDirection,
    SyncOptions,
)
from hagency_cli.files.sync.planning import build_sync_plan
from hagency_cli.files.sync.selection import (
    IgnoreMatcher,
    _filter_actions_for_git_paths,
    scan_local,
)


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
                ".git",
                PROTECTED_CONFIG_PATTERN,
            )
            ignore = IgnoreMatcher(
                patterns, tuple(dict.fromkeys(protected_paths)), protect_git=True
            )
            _validate_bundle_source(manifest, ignore)
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

    emit(f"bundle {'plan' if dry_run else 'applied'}: {len(actions)} planned action(s)")
    return ApplyReport(
        bundle_path=bundle,
        target_root=target,
        manifest=manifest,
        actions=tuple(actions),
        dry_run=dry_run,
    )
