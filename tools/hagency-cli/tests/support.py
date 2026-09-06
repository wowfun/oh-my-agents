"""Filesystem-backed SFTP fixtures shared by regression suites."""

import os
import shutil
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from hagency_cli.files.sync.sftp import SFTPRemote


class LocalSFTPAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, remote_path: str) -> Path:
        normalized = PurePosixPath(remote_path.replace("\\", "/"))
        return self.root.joinpath(
            *(part for part in normalized.parts if part not in {"/", "."})
        )

    @staticmethod
    def _attrs(path: Path, filename: str | None = None):
        info = path.lstat()
        return SimpleNamespace(
            filename=filename or path.name,
            st_mode=info.st_mode,
            st_size=info.st_size,
            st_mtime=info.st_mtime,
        )

    def lstat(self, remote_path: str):
        return self._attrs(self._path(remote_path))

    def listdir_attr(self, remote_path: str):
        path = self._path(remote_path)
        return [self._attrs(child) for child in path.iterdir()]

    def listdir(self, remote_path: str):
        return [child.name for child in self._path(remote_path).iterdir()]

    def readlink(self, remote_path: str):
        return os.readlink(self._path(remote_path))

    def mkdir(self, remote_path: str, mode: int | None = None):
        path = self._path(remote_path)
        path.mkdir()
        if mode is not None:
            path.chmod(mode)

    def chmod(self, remote_path: str, mode: int):
        self._path(remote_path).chmod(mode)

    def remove(self, remote_path: str):
        self._path(remote_path).unlink()

    def rmdir(self, remote_path: str):
        self._path(remote_path).rmdir()

    def put(self, local_path: str, remote_path: str, *, confirm: bool):
        self.assert_confirm(confirm)
        shutil.copyfile(local_path, self._path(remote_path))

    def get(self, remote_path: str, local_path: str):
        shutil.copyfile(self._path(remote_path), local_path)

    def open(self, remote_path: str, mode: str):
        return self._path(remote_path).open(mode)

    def utime(self, remote_path: str, times: tuple[float, float]):
        os.utime(self._path(remote_path), times)

    def rename(self, source: str, destination: str):
        self._path(source).rename(self._path(destination))

    def posix_rename(self, source: str, destination: str):
        os.replace(self._path(source), self._path(destination))

    def symlink(self, target: str, remote_path: str):
        os.symlink(target, self._path(remote_path))

    @staticmethod
    def assert_confirm(confirm: bool) -> None:
        if not confirm:
            raise AssertionError("SFTP uploads must confirm the remote stat")


class LocalSFTPRemote(SFTPRemote):
    def __init__(self, config, root: Path) -> None:
        super().__init__(config)
        self.adapter = LocalSFTPAdapter(root)

    def __enter__(self):
        self._sftp = self.adapter
        return self

    def __exit__(self, *_args):
        self._sftp = None
