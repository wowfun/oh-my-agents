from __future__ import annotations

import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from hagency_cli.files.purge.inspection import (
    _context_allows,
    _discover_git_contexts,
    _find_project_root,
    _git_tracked_state,
    _is_link_or_reparse,
    _is_reparse_stat,
    _is_within,
    _measure_candidate,
    _same_identity,
    _valid_cachedir_tag,
)
from hagency_cli.files.purge.models import (
    CACHEDIR_TAG_NAME,
    PURGE_TARGETS,
    Activity,
    _Identity,
    _PlannedCandidate,
)


def _revalidate(candidate: _PlannedCandidate) -> str | None:
    path = candidate.choice.exact_path
    if not _same_identity(candidate.root, candidate.root_identity):
        return "scan root changed after review"
    if not _same_identity(path.parent, candidate.parent_identity):
        return "parent directory changed after review"
    if not _same_identity(path, candidate.target_identity):
        return "candidate changed after review"
    try:
        if _is_link_or_reparse(path) or not _is_within(
            path.resolve(), candidate.root.resolve()
        ):
            return "candidate is no longer a safe real directory"
    except OSError:
        return "candidate can no longer be resolved safely"

    if candidate.choice.artifact_kind == CACHEDIR_TAG_NAME:
        if not _valid_cachedir_tag(path):
            return "candidate no longer has a valid CACHEDIR.TAG"
    elif path.name != candidate.choice.artifact_kind or path.name not in PURGE_TARGETS:
        return "candidate no longer matches the purge catalog"

    project_root = _find_project_root(path, candidate.root)
    if project_root is None or not _context_allows(path, project_root):
        return "candidate no longer has a safe project context"
    if project_root.resolve() != candidate.choice.project_path:
        return "candidate project ownership changed after review"

    git_contexts, git_error = _discover_git_contexts(path)
    if git_error is not None:
        return f"Git safety check failed: {git_error}"
    if git_contexts != candidate.git_contexts:
        return "candidate Git repository identity changed after review"
    for git_context in git_contexts:
        tracked_path = (
            path
            if _is_within(path, git_context.root, allow_equal=True)
            else git_context.root
        )
        tracked, git_error = _git_tracked_state(tracked_path, git_context.root)
        if tracked is None:
            return f"Git safety check failed: {git_error}"
        if tracked:
            return "candidate now contains Git-tracked content"

    _size, activity, measure_error, _entries = _measure_candidate(path, time.time())
    if measure_error is not None:
        return f"activity safety check failed: {measure_error}"
    if candidate.choice.activity is Activity.OLD and activity is not Activity.OLD:
        return "candidate activity changed after review"
    if (
        candidate.choice.activity is not Activity.UNCERTAIN
        and activity is Activity.UNCERTAIN
    ):
        return "candidate activity can no longer be verified"
    return None


def _remove_tree_no_follow(
    path: Path, expected_identity: _Identity | None = None
) -> None:
    stack: list[tuple[Path, _Identity | None, bool]] = [
        (path, expected_identity, False)
    ]
    while stack:
        current, expected, visited = stack.pop()
        info = current.stat(follow_symlinks=False)
        identity = _Identity(info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if (
            not identity.inode
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _is_reparse_stat(info)
        ):
            raise OSError(f"refusing to recurse into unsafe directory: {current}")
        if expected is not None and identity != expected:
            raise OSError(f"directory identity changed during removal: {current}")
        if visited:
            current.rmdir()
            continue

        with os.scandir(current) as entries:
            children = [Path(entry.path) for entry in entries]
        stack.append((current, identity, True))
        for child in reversed(children):
            child_info = child.stat(follow_symlinks=False)
            child_identity = _Identity(
                child_info.st_dev,
                child_info.st_ino,
                stat.S_IFMT(child_info.st_mode),
            )
            if stat.S_ISDIR(child_info.st_mode) and not _is_reparse_stat(child_info):
                stack.append((child, child_identity, False))
            elif stat.S_ISDIR(child_info.st_mode) and _is_reparse_stat(child_info):
                child.rmdir()
            else:
                child.unlink()


@dataclass
class _FdRemovalFrame:
    directory_fd: int
    parent_fd: int | None
    name: str | None
    expected_identity: _Identity
    names: list[str]
    index: int = 0
    owns_fd: bool = True


def _remove_tree_from_fd(directory_fd: int) -> None:
    root_info = os.fstat(directory_fd)
    frames = [
        _FdRemovalFrame(
            directory_fd=directory_fd,
            parent_fd=None,
            name=None,
            expected_identity=_Identity(
                root_info.st_dev,
                root_info.st_ino,
                stat.S_IFMT(root_info.st_mode),
            ),
            names=os.listdir(directory_fd),
            owns_fd=False,
        )
    ]
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        while frames:
            frame = frames[-1]
            if frame.index >= len(frame.names):
                try:
                    if frame.parent_fd is not None and frame.name is not None:
                        final_info = os.stat(
                            frame.name,
                            dir_fd=frame.parent_fd,
                            follow_symlinks=False,
                        )
                        final_identity = _Identity(
                            final_info.st_dev,
                            final_info.st_ino,
                            stat.S_IFMT(final_info.st_mode),
                        )
                        if final_identity != frame.expected_identity:
                            raise OSError(
                                "directory identity changed during removal: "
                                f"{frame.name}"
                            )
                        os.rmdir(frame.name, dir_fd=frame.parent_fd)
                finally:
                    if frame.owns_fd:
                        os.close(frame.directory_fd)
                        frame.owns_fd = False
                frames.pop()
                continue

            name = frame.names[frame.index]
            frame.index += 1
            info = os.stat(name, dir_fd=frame.directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not _is_reparse_stat(info):
                child_fd = os.open(name, flags, dir_fd=frame.directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    expected = _Identity(
                        info.st_dev,
                        info.st_ino,
                        stat.S_IFMT(info.st_mode),
                    )
                    actual = _Identity(
                        opened.st_dev,
                        opened.st_ino,
                        stat.S_IFMT(opened.st_mode),
                    )
                    if not actual.inode or actual != expected:
                        raise OSError(
                            f"directory identity changed during removal: {name}"
                        )
                    names = os.listdir(child_fd)
                except BaseException:
                    os.close(child_fd)
                    raise
                frames.append(
                    _FdRemovalFrame(
                        directory_fd=child_fd,
                        parent_fd=frame.directory_fd,
                        name=name,
                        expected_identity=expected,
                        names=names,
                    )
                )
            elif stat.S_ISDIR(info.st_mode) and _is_reparse_stat(info):
                os.rmdir(name, dir_fd=frame.directory_fd)
            else:
                os.unlink(name, dir_fd=frame.directory_fd)
    finally:
        for frame in frames:
            if frame.owns_fd:
                os.close(frame.directory_fd)
                frame.owns_fd = False


def _permanently_remove(candidate: _PlannedCandidate) -> None:
    path = candidate.choice.exact_path
    has_fd_removal = os.listdir in os.supports_fd and all(
        function in os.supports_dir_fd
        for function in (os.open, os.stat, os.unlink, os.rmdir, os.rename)
    )
    if os.name == "nt" or not has_fd_removal:
        if not _same_identity(path.parent, candidate.parent_identity):
            raise OSError("parent identity changed immediately before removal")
        if not _same_identity(path, candidate.target_identity):
            raise OSError("candidate identity changed immediately before removal")
        quarantine = path.parent / f".hagency-purge-{uuid.uuid4().hex}"
        os.replace(path, quarantine)
        try:
            if not _same_identity(quarantine, candidate.target_identity):
                raise OSError("candidate identity changed before atomic quarantine")
            _remove_tree_no_follow(quarantine, candidate.target_identity)
        except BaseException as exc:
            recovery = ""
            if os.path.lexists(quarantine) and not os.path.lexists(path):
                try:
                    os.replace(quarantine, path)
                except OSError:
                    recovery = f"; remaining data is at {quarantine}"
            elif os.path.lexists(quarantine):
                recovery = f"; remaining data is at {quarantine}"
            if isinstance(exc, OSError):
                raise OSError(f"{exc}{recovery}") from exc
            if recovery and hasattr(exc, "add_note"):
                exc.add_note(recovery.removeprefix("; "))
            raise
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, flags)
    try:
        parent_info = os.fstat(parent_fd)
        if (
            _Identity(
                parent_info.st_dev,
                parent_info.st_ino,
                stat.S_IFMT(parent_info.st_mode),
            )
            != candidate.parent_identity
        ):
            raise OSError("parent identity changed immediately before removal")
        target_info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _Identity(
                target_info.st_dev,
                target_info.st_ino,
                stat.S_IFMT(target_info.st_mode),
            )
            != candidate.target_identity
        ):
            raise OSError("candidate identity changed immediately before removal")
        if not stat.S_ISDIR(target_info.st_mode) or stat.S_ISLNK(target_info.st_mode):
            raise OSError("candidate is no longer a real directory")

        target_fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(target_fd)
            if (opened.st_dev, opened.st_ino) != (
                target_info.st_dev,
                target_info.st_ino,
            ):
                raise OSError("candidate identity changed during removal")
            _remove_tree_from_fd(target_fd)
            # Claim a private name before the final unlink. Concurrent builds
            # may recreate the original artifact path after it becomes empty;
            # rmdir must never remove that newly created directory instead.
            quarantine_name = f".hagency-purge-{uuid.uuid4().hex}"
            os.rename(
                path.name,
                quarantine_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            quarantine = path.with_name(quarantine_name)
            try:
                final_info = os.stat(
                    quarantine_name, dir_fd=parent_fd, follow_symlinks=False
                )
                if (final_info.st_dev, final_info.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise OSError("candidate identity changed before final removal")
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except BaseException as exc:
                # Retain failed quarantine contents rather than restoring over
                # a concurrently recreated artifact. Report their recovery path.
                recovery = f"; inspect remaining data at {quarantine}"
                if isinstance(exc, OSError):
                    raise OSError(f"{exc}{recovery}") from exc
                if hasattr(exc, "add_note"):
                    exc.add_note(recovery.removeprefix("; "))
                raise
        finally:
            os.close(target_fd)
    finally:
        os.close(parent_fd)
