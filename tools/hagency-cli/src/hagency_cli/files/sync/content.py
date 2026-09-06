from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from hagency_cli.files.sync.models import (
    CONTENT_CHUNK_SIZE,
    ActionKind,
    EntryKind,
    FileSyncError,
    RemoteFileSystem,
    SyncAction,
)


@dataclass(frozen=True)
class _ContentDigest:
    raw: bytes
    normalized: bytes
    contains_nul: bool


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
