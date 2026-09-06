from __future__ import annotations

import errno
import shutil
import stat
from pathlib import Path

from hagency_cli.files.sync.models import EntryKind, FileEntry


def _remove_local_for_replace(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _delete_local(path: Path, entry: FileEntry | None) -> None:
    try:
        if entry is not None and entry.kind is EntryKind.DIRECTORY:
            path.rmdir()
        else:
            path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        if (
            entry is not None
            and entry.kind is EntryKind.DIRECTORY
            and exc.errno
            in {
                errno.ENOTEMPTY,
                errno.EEXIST,
            }
        ):
            return
        raise
