from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from hagency_cli.files.sync.bundle.format import (
    _bundle_manifest_payload,
    _bundle_payload_path,
    _zip_info,
)
from hagency_cli.files.sync.config import load_local_sync_selection
from hagency_cli.files.sync.content import _read_stable_local_file
from hagency_cli.files.sync.models import (
    BUNDLE_MANIFEST_PATH,
    DEFAULT_BUNDLE_FILENAME,
    BundleEntry,
    BundleManifest,
    BundleMode,
    EntryKind,
    FileSyncConfigError,
    FileSyncError,
    LocalSyncSelection,
    PackReport,
    Progress,
    Snapshot,
)
from hagency_cli.files.sync.selection import (
    IgnoreMatcher,
    _ignored_bundle_path,
    git_changed_paths,
    scan_local,
)


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
        if _ignored_bundle_path(path, ignore):
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
    ignore = IgnoreMatcher(
        selection.ignore_patterns, selection.protected_paths, protect_git=True
    )
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
