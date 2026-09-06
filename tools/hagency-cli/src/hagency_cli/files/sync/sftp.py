from __future__ import annotations

import getpass
import io
import os
import posixpath
import re
import secrets
import socket
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Self

from hagency_cli.files.sync.content import _content_digest, _content_digests_equal
from hagency_cli.files.sync.local import _delete_local, _remove_local_for_replace
from hagency_cli.files.sync.models import (
    ActionKind,
    EntryKind,
    FileEntry,
    FileSyncConfigError,
    FileSyncError,
    SFTPConfig,
    Snapshot,
    SyncAction,
)
from hagency_cli.files.sync.selection import IgnoreMatcher, _scan_path_selection


def _connect_agent(path: str, timeout: float):
    """Select a connection-local agent without changing SSH_AUTH_SOCK."""
    from paramiko import Agent
    from paramiko.agent import AgentSSH

    if sys.platform == "win32":
        # Paramiko uses native Pageant/OpenSSH discovery on Windows and has
        # always ignored SSH_AUTH_SOCK there. Preserve that platform behavior.
        return Agent()

    class SocketAgent(AgentSSH):
        def close(self):
            self._close()

    agent = SocketAgent()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(path)
    except OSError:
        connection.close()
        # Match Paramiko's missing-agent behavior: continue trying key files
        # and password authentication with an empty agent key list.
        return agent
    except BaseException:
        connection.close()
        raise
    try:
        agent._connect(connection)
    except BaseException:
        connection.close()
        raise
    return agent


class SFTPRemote:
    def __init__(self, config: SFTPConfig) -> None:
        self.config = config
        self._client = None
        self._sftp = None
        self._proxy = None
        self._agent = None
        self._confirmed_directories: set[str] = set()

    def __enter__(self) -> Self:
        self._confirmed_directories.clear()
        try:
            import paramiko

            host = self.config.host
            port = self.config.port
            username = self.config.username
            key_filename: str | list[str] | None = (
                str(self.config.private_key_path)
                if self.config.private_key_path is not None
                else None
            )
            proxy_command = None
            if self.config.ssh_config_path.is_file():
                ssh_config = paramiko.SSHConfig()
                explicit_ssh_values = ["Host *"]
                if self.config.config_path is None:
                    if port is not None:
                        explicit_ssh_values.append(f"  Port {port}")
                    if username is not None:
                        explicit_ssh_values.append(f"  User {username}")
                if len(explicit_ssh_values) > 1:
                    ssh_config.parse(io.StringIO("\n".join(explicit_ssh_values)))
                with self.config.ssh_config_path.open(encoding="utf-8") as handle:
                    ssh_config.parse(handle)
                resolved = ssh_config.lookup(host)
                host = resolved.get("hostname", host)
                port = port or int(resolved.get("port", 22))
                username = username or resolved.get("user")
                key_filename = key_filename or resolved.get("identityfile")
                proxy_command = resolved.get("proxycommand")
            port = port or 22
            username = username or getpass.getuser()

            agent = self.config.agent
            if agent:
                if agent.startswith("$"):
                    variable = agent[1:]
                    agent = os.environ.get(variable, "")
                    if not agent:
                        raise FileSyncConfigError(
                            f"environment variable {variable!r} referenced by agent "
                            "is not set"
                        )

            passphrase = self.config.passphrase
            if passphrase is True:
                passphrase = getpass.getpass(
                    f"[{self.config.host}] private key passphrase: "
                )
            if proxy_command and proxy_command.lower() != "none":
                self._proxy = paramiko.ProxyCommand(proxy_command)

            client = paramiko.SSHClient()
            self._client = client
            client.load_system_host_keys()
            if agent:
                self._agent = _connect_agent(agent, self.config.connect_timeout)
                # Paramiko 3.5/4.x has no public per-client agent option. Its
                # legacy authentication routine reuses _agent when supplied,
                # retaining key-file, agent, and password fallback ordering.
                client._agent = self._agent
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=self.config.password,
                key_filename=key_filename,
                passphrase=passphrase if isinstance(passphrase, str) else None,
                timeout=self.config.connect_timeout,
                banner_timeout=self.config.connect_timeout,
                auth_timeout=max(self.config.connect_timeout, 60.0)
                if self.config.passphrase is True
                else self.config.connect_timeout,
                allow_agent=True,
                look_for_keys=True,
                sock=self._proxy,
            )
            self._sftp = client.open_sftp()
            return self
        except FileSyncConfigError:
            self.__exit__(None, None, None)
            raise
        except Exception as exc:
            self.__exit__(None, None, None)
            raise FileSyncError(
                f"SFTP connection failed for {self.config.endpoint}: {exc}"
            ) from exc
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type, _value, _traceback) -> None:
        self._confirmed_directories.clear()
        try:
            if self._sftp is not None:
                self._sftp.close()
        finally:
            self._sftp = None
            try:
                if self._client is not None:
                    self._client.close()
            finally:
                self._client = None
                try:
                    # SSHClient.close returns early if no transport was
                    # created; our explicitly opened agent still needs closing.
                    if self._agent is not None:
                        self._agent.close()
                finally:
                    self._agent = None
                    if self._proxy is not None:
                        self._proxy.close()
                    self._proxy = None

    @property
    def sftp(self):
        if self._sftp is None:
            raise FileSyncError("SFTP session is not connected")
        return self._sftp

    def _path(self, relative: PurePosixPath | None = None) -> str:
        if relative is None or not relative.parts:
            return self.config.remote_root
        return posixpath.join(self.config.remote_root, relative.as_posix())

    @staticmethod
    def _missing(error: BaseException) -> bool:
        return (
            isinstance(error, FileNotFoundError) or getattr(error, "errno", None) == 2
        )

    @staticmethod
    def _entry_kind(mode: int) -> EntryKind | None:
        if stat.S_ISDIR(mode):
            return EntryKind.DIRECTORY
        if stat.S_ISLNK(mode):
            return EntryKind.SYMLINK
        if stat.S_ISREG(mode):
            return EntryKind.FILE
        return None

    @staticmethod
    def _safe_child_name(parent: str, value: object) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\0" in value
        ):
            raise FileSyncError(
                f"unsafe remote directory entry {value!r} under {parent}"
            )
        return value

    def snapshot(
        self,
        ignore: IgnoreMatcher,
        *,
        paths: frozenset[PurePosixPath] | None = None,
    ) -> Snapshot:
        self._confirmed_directories.clear()
        try:
            root_info = self.sftp.lstat(self.config.remote_root)
        except OSError as exc:
            if self._missing(exc):
                return Snapshot(False, {})
            raise FileSyncError(
                f"cannot stat remote path {self.config.remote_root}: {exc}"
            ) from exc
        if not stat.S_ISDIR(root_info.st_mode):
            raise FileSyncError(
                f"remote path is not a directory: {self.config.remote_root}"
            )

        entries: dict[PurePosixPath, FileEntry] = {}
        selected_paths = _scan_path_selection(paths)
        if selected_paths == frozenset():
            return Snapshot(True, entries)

        pending: list[tuple[str, PurePosixPath | None]] = [
            (self.config.remote_root, None)
        ]
        while pending:
            remote_dir, relative_dir = pending.pop()
            try:
                children = sorted(
                    self.sftp.listdir_attr(remote_dir), key=lambda item: item.filename
                )
            except OSError as exc:
                raise FileSyncError(
                    f"cannot list remote directory {remote_dir}: {exc}"
                ) from exc
            for child in children:
                child_name = self._safe_child_name(remote_dir, child.filename)
                relative = (
                    PurePosixPath(child_name)
                    if relative_dir is None
                    else relative_dir / child_name
                )
                if selected_paths is not None and relative not in selected_paths:
                    continue
                kind = self._entry_kind(child.st_mode)
                if kind is None:
                    continue
                if ignore.matches(relative, directory=kind is EntryKind.DIRECTORY):
                    continue
                remote_path = posixpath.join(remote_dir, child_name)
                try:
                    link_target = (
                        self.sftp.readlink(remote_path)
                        if kind is EntryKind.SYMLINK
                        else None
                    )
                except OSError as exc:
                    raise FileSyncError(
                        f"cannot read remote symlink {remote_path}: {exc}"
                    ) from exc
                entries[relative] = FileEntry(
                    kind=kind,
                    size=child.st_size,
                    mtime=child.st_mtime - self.config.remote_time_offset,
                    mode=stat.S_IMODE(child.st_mode),
                    link_target=link_target,
                )
                if kind is EntryKind.DIRECTORY:
                    pending.append((remote_path, relative))

        return Snapshot(True, entries)

    def equivalent_file_content(
        self, relative: PurePosixPath, local_root: Path
    ) -> bool:
        local_path = local_root.joinpath(*relative.parts)
        remote_path = self._path(relative)
        try:
            with local_path.open("rb") as local_handle:
                local_digest = _content_digest(local_handle)
            with self.sftp.open(remote_path, "rb") as remote_handle:
                remote_digest = _content_digest(remote_handle)
        except Exception as exc:
            raise FileSyncError(
                f"cannot compare local and remote content for {relative.as_posix()}: "
                f"{exc}"
            ) from exc
        return _content_digests_equal(local_digest, remote_digest)

    def _lstat(self, path: str):
        try:
            return self.sftp.lstat(path)
        except OSError as exc:
            if self._missing(exc):
                return None
            raise

    @staticmethod
    def _is_remote_root(path: str) -> bool:
        return path in {"", ".", "/"} or bool(re.fullmatch(r"[A-Za-z]:/?", path))

    def _ensure_directory(self, path: str, mode: int | None = None) -> None:
        if path in self._confirmed_directories:
            return
        info = self._lstat(path)
        if info is not None:
            if stat.S_ISDIR(info.st_mode):
                self._confirmed_directories.add(path)
                return
            self._remove_tree(path, info.st_mode)
        if self._is_remote_root(path):
            return
        parent = posixpath.dirname(path)
        if parent != path:
            self._ensure_directory(parent, mode)
        if mode is None:
            self.sftp.mkdir(path)
        else:
            self.sftp.mkdir(path, mode=mode)
        if mode is not None:
            self.sftp.chmod(path, mode)
        self._confirmed_directories.add(path)

    def ensure_root(self) -> None:
        self._ensure_directory(self.config.remote_root, self.config.dir_perm)

    def _remove_tree(self, path: str, mode: int | None = None) -> None:
        self._confirmed_directories.discard(path)
        info = self._lstat(path) if mode is None else None
        actual_mode = info.st_mode if info is not None else mode
        if actual_mode is None:
            return
        if stat.S_ISDIR(actual_mode):
            # Replacing a directory invalidates every cached descendant too.
            self._confirmed_directories.clear()
            for child in self.sftp.listdir_attr(path):
                child_name = self._safe_child_name(path, child.filename)
                child_path = posixpath.join(path, child_name)
                self._remove_tree(child_path, child.st_mode)
            self.sftp.rmdir(path)
        else:
            self.sftp.remove(path)

    def _delete(self, path: str, entry: FileEntry | None) -> None:
        self._confirmed_directories.discard(path)
        if entry is not None and entry.kind is EntryKind.DIRECTORY:
            self._confirmed_directories.clear()
        try:
            if entry is not None and entry.kind is EntryKind.DIRECTORY:
                self.sftp.rmdir(path)
            else:
                self.sftp.remove(path)
        except OSError as exc:
            if self._missing(exc):
                return
            if entry is not None and entry.kind is EntryKind.DIRECTORY:
                try:
                    if self.sftp.listdir(path):
                        return
                except OSError:
                    pass
            raise

    def _prepare_destination(self, path: str, desired: EntryKind) -> None:
        info = self._lstat(path)
        if info is None:
            self._confirmed_directories.discard(path)
            return
        current = self._entry_kind(info.st_mode)
        if current is desired and desired in {EntryKind.FILE, EntryKind.DIRECTORY}:
            return
        self._remove_tree(path, info.st_mode)

    def _temporary_upload_path(self, remote_path: str) -> str:
        parent = posixpath.dirname(remote_path)
        for _attempt in range(16):
            candidate = posixpath.join(
                parent, f".hgc-upload-{secrets.token_hex(16)}.tmp"
            )
            if self._lstat(candidate) is None:
                return candidate
        raise FileSyncError(
            f"cannot allocate a unique remote upload path beside {remote_path}"
        )

    def _upload_file(
        self, local_path: Path, remote_path: str, action: SyncAction
    ) -> None:
        self._ensure_directory(posixpath.dirname(remote_path), self.config.dir_perm)
        self._prepare_destination(remote_path, EntryKind.FILE)
        upload_path = (
            self._temporary_upload_path(remote_path)
            if self.config.use_temp_file
            else remote_path
        )
        try:
            self.sftp.put(str(local_path), upload_path, confirm=True)
            source = action.source
            if source is not None:
                remote_time = source.mtime + self.config.remote_time_offset
                self.sftp.utime(upload_path, (remote_time, remote_time))
                mode = self.config.file_perm
                if mode is None and action.existing is not None:
                    mode = action.existing.mode
                if mode is None:
                    mode = source.mode
                if mode is not None:
                    self.sftp.chmod(upload_path, mode)
            if upload_path != remote_path:
                if self.config.open_ssh:
                    self.sftp.posix_rename(upload_path, remote_path)
                else:
                    self._remove_tree(remote_path)
                    self.sftp.rename(upload_path, remote_path)
        except Exception:
            if upload_path != remote_path:
                try:
                    self._remove_tree(upload_path)
                except Exception:
                    pass
            raise

    def _upload_symlink(
        self, local_path: Path, remote_path: str, action: SyncAction
    ) -> None:
        self._ensure_directory(posixpath.dirname(remote_path), self.config.dir_perm)
        self._prepare_destination(remote_path, EntryKind.SYMLINK)
        target = action.source.link_target if action.source else os.readlink(local_path)
        if target is None:
            raise FileSyncError(f"cannot read local symlink target: {local_path}")
        self.sftp.symlink(target, remote_path)

    def _download_file(
        self, remote_path: str, local_path: Path, action: SyncAction
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            info = local_path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            _remove_local_for_replace(local_path)
            info = None
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{local_path.name}.hgc-", dir=local_path.parent
        )
        os.close(fd)
        temp_path = Path(temp_name)
        try:
            self.sftp.get(remote_path, str(temp_path))
            source = action.source
            if source is not None:
                os.utime(temp_path, (source.mtime, source.mtime))
                mode = stat.S_IMODE(info.st_mode) if info is not None else source.mode
                if mode is not None:
                    os.chmod(temp_path, mode)
            os.replace(temp_path, local_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def _download_symlink(
        self, remote_path: str, local_path: Path, action: SyncAction
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _remove_local_for_replace(local_path)
        target = (
            action.source.link_target
            if action.source
            else self.sftp.readlink(remote_path)
        )
        if target is None:
            raise FileSyncError(f"cannot read remote symlink target: {remote_path}")
        os.symlink(target, local_path)

    def apply(self, action: SyncAction, local_root: Path) -> None:
        local_path = local_root.joinpath(*action.path.parts)
        remote_path = self._path(action.path)
        if action.kind is ActionKind.DELETE_REMOTE:
            self._delete(remote_path, action.existing)
        elif action.kind is ActionKind.DELETE_LOCAL:
            _delete_local(local_path, action.existing)
        elif action.kind is ActionKind.CREATE_REMOTE_DIRECTORY:
            self._prepare_destination(remote_path, EntryKind.DIRECTORY)
            self._ensure_directory(remote_path, self.config.dir_perm)
        elif action.kind is ActionKind.CREATE_LOCAL_DIRECTORY:
            try:
                is_directory = local_path.is_dir() and not local_path.is_symlink()
            except OSError:
                is_directory = False
            if not is_directory and (local_path.exists() or local_path.is_symlink()):
                _remove_local_for_replace(local_path)
            local_path.mkdir(parents=True, exist_ok=True)
        elif action.kind is ActionKind.COPY_TO_REMOTE:
            if action.source is not None and action.source.kind is EntryKind.SYMLINK:
                self._upload_symlink(local_path, remote_path, action)
            else:
                self._upload_file(local_path, remote_path, action)
        elif action.kind is ActionKind.COPY_TO_LOCAL:
            if action.source is not None and action.source.kind is EntryKind.SYMLINK:
                self._download_symlink(remote_path, local_path, action)
            else:
                self._download_file(remote_path, local_path, action)
