from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest import mock

from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hagency_cli import cli
from hagency_cli import file_sync as file_sync_module
from hagency_cli.file_sync import (
    BUNDLE_FORMAT,
    BUNDLE_MANIFEST_PATH,
    BUNDLE_VERSION,
    CONTENT_CHUNK_SIZE,
    ActionKind,
    BundleMode,
    EntryKind,
    FileEntry,
    FileSyncConfigError,
    FileSyncError,
    IgnoreMatcher,
    RemoteEndpoint,
    SFTPRemote,
    Snapshot,
    SyncAction,
    SyncDirection,
    SyncOptions,
    SyncReport,
    apply_sync_bundle,
    build_sync_plan,
    build_temporary_sftp_config,
    git_changed_paths,
    initialize_sftp_config,
    load_local_sync_selection,
    load_sftp_config,
    pack_sync_bundle,
    parse_remote_endpoint,
    render_default_sftp_config,
    scan_local,
    sync_workspace_files,
    verify_sync_bundle,
)


def file_entry(*, mtime: float = 1, size: int = 1) -> FileEntry:
    return FileEntry(EntryKind.FILE, size=size, mtime=mtime, mode=0o644)


def directory_entry(*, mtime: float = 1) -> FileEntry:
    return FileEntry(EntryKind.DIRECTORY, size=0, mtime=mtime, mode=0o755)


def snapshot(entries: dict[str, FileEntry], *, exists: bool = True) -> Snapshot:
    return Snapshot(
        exists=exists,
        entries={PurePosixPath(path): entry for path, entry in entries.items()},
    )


class FakeRemote:
    def __init__(self, remote_snapshot: Snapshot) -> None:
        self.remote_snapshot = remote_snapshot
        self.ensure_root_calls = 0
        self.applied = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def snapshot(self, _ignore):
        return self.remote_snapshot

    def ensure_root(self):
        self.ensure_root_calls += 1

    def apply(self, action, local_root):
        self.applied.append((action, local_root))


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


class FileSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".vscode").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @unittest.skipUnless(os.name == "posix", "requires long filesystem paths")
    def test_scanners_handle_trees_deeper_than_python_recursion_limit(self) -> None:
        base = self.root / "deep"
        base.mkdir()
        depth = sys.getrecursionlimit() + 25
        path_limit = os.pathconf(base, "PC_PATH_MAX")
        if 0 < path_limit <= len(os.fsencode(base)) + 2 * depth + len("/file.txt"):
            self.skipTest("filesystem path limit is below Python recursion depth")
        directories = []
        current = base
        leaf = None
        try:
            for _index in range(depth):
                current = current / "d"
                current.mkdir()
                directories.append(current)
            leaf = current / "file.txt"
            leaf.write_text("deep file", encoding="utf-8")
            relative = PurePosixPath(leaf.relative_to(base).as_posix())
            remote = LocalSFTPRemote(
                build_temporary_sftp_config(base, "server:."), base
            )
            with remote:
                for scan in (
                    lambda: scan_local(base, IgnoreMatcher(())),
                    lambda: remote.snapshot(IgnoreMatcher(())),
                ):
                    with self.subTest(scan=scan):
                        result = scan()
                        self.assertEqual(result.entries[relative].kind, EntryKind.FILE)
                        self.assertEqual(len(result.entries), len(directories) + 1)
        finally:
            if leaf is not None:
                leaf.unlink(missing_ok=True)
            for directory in reversed(directories):
                directory.rmdir()

    def test_oversized_bundle_manifest_is_rejected_before_decompression(self) -> None:
        source = self.root / "source"
        source.mkdir()
        bundle = self.root / "oversized.zip"
        pack_sync_bundle(source, no_config=True, output_path=bundle)
        manifest = json.dumps(self.read_bundle_manifest(bundle)).encode()
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(BUNDLE_MANIFEST_PATH, manifest + b" " * (1024 * 1024))
        target = self.root / "target"
        with (
            mock.patch.object(file_sync_module, "BUNDLE_MANIFEST_MAX_BYTES", 1024),
            mock.patch.object(zipfile.ZipFile, "open") as open_entry,
            self.assertRaisesRegex(FileSyncConfigError, "manifest.*exceeds"),
        ):
            apply_sync_bundle(bundle, target)
        open_entry.assert_not_called()
        self.assertFalse(target.exists())

    def test_deeply_nested_bundle_manifest_reports_a_config_error(self) -> None:
        bundle = self.root / "nested.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr(
                BUNDLE_MANIFEST_PATH,
                b"[" * (sys.getrecursionlimit() + 10)
                + b"0"
                + b"]" * (sys.getrecursionlimit() + 10),
            )
        with self.assertRaisesRegex(FileSyncConfigError, "manifest"):
            verify_sync_bundle(bundle)

    def test_pack_enforces_manifest_limit_without_replacing_existing_output(
        self,
    ) -> None:
        source = self.root / "limit-source"
        source.mkdir()
        (source / "file.txt").write_text("data", encoding="utf-8")
        bundle = self.root / "limit.zip"
        bundle.write_bytes(b"original")
        for dry_run in (True, False):
            with (
                self.subTest(dry_run=dry_run),
                mock.patch.object(file_sync_module, "BUNDLE_MANIFEST_MAX_BYTES", 32),
                self.assertRaisesRegex(FileSyncConfigError, "manifest.*exceeds"),
            ):
                pack_sync_bundle(
                    source,
                    no_config=True,
                    output_path=bundle,
                    force=True,
                    dry_run=dry_run,
                )
            self.assertEqual(bundle.read_bytes(), b"original")
            self.assertEqual(list(self.root.glob(".limit.zip.hgc-*")), [])

    def test_size_mismatch_skips_remote_content_only_when_normalization_cannot_match(
        self,
    ) -> None:
        for local_data, remote_data, equal in (
            (b"\r\n" * 10, b"\n" * 10, True),
            (b"x" * 21, b"x" * 10, False),
            (b"", b"x", False),
        ):
            with self.subTest(local_data=local_data, remote_data=remote_data):
                project, store, _remote_file = self.create_content_project(
                    f"size-{len(local_data)}", local_data, remote_data
                )
                remote = LocalSFTPRemote(load_sftp_config(project), store)
                with mock.patch.object(
                    remote.adapter, "open", wraps=remote.adapter.open
                ) as open_remote:
                    report = sync_workspace_files(
                        project,
                        SyncDirection.LOCAL_TO_REMOTE,
                        dry_run=True,
                        remote_factory=lambda _config, remote=remote: remote,
                    )
                self.assertEqual(bool(report.actions), not equal)
                self.assertEqual(open_remote.call_count, int(equal))

    def test_git_changed_sync_prunes_unrelated_local_and_remote_directories(
        self,
    ) -> None:
        project, remote_store, remote_file = self.create_content_project(
            "pruned-sync", b"new content", b"old"
        )
        unrelated = project / "unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("local", encoding="utf-8")
        remote_unrelated = remote_file.parent / "unrelated"
        remote_unrelated.mkdir()
        (remote_unrelated / "keep.txt").write_text("remote", encoding="utf-8")
        remote = LocalSFTPRemote(load_sftp_config(project), remote_store)
        with (
            mock.patch.object(
                file_sync_module,
                "git_changed_paths",
                return_value=frozenset({PurePosixPath("shared.txt")}),
            ),
            mock.patch.object(os, "scandir", wraps=os.scandir) as scandir,
            mock.patch.object(
                remote.adapter, "listdir_attr", wraps=remote.adapter.listdir_attr
            ) as listdir,
        ):
            report = sync_workspace_files(
                project,
                SyncDirection.LOCAL_TO_REMOTE,
                git_changed=True,
                remote_factory=lambda _config: remote,
            )
        self.assertEqual(len(report.actions), 1)
        self.assertEqual(remote_file.read_bytes(), b"new content")
        self.assertNotIn(mock.call(unrelated), scandir.call_args_list)
        self.assertNotIn(mock.call("/remote/unrelated"), listdir.call_args_list)
        self.assertEqual((remote_unrelated / "keep.txt").read_text(), "remote")

    def test_git_patch_pack_and_apply_prune_unrelated_directories(self) -> None:
        source = self.root / "patch-source"
        target = self.root / "patch-target"
        for root in (source, target):
            (root / "changed").mkdir(parents=True)
            (root / "unrelated").mkdir()
            (root / "unrelated" / "keep.txt").write_text("keep", encoding="utf-8")
        (source / "changed" / "file.txt").write_text("new content", encoding="utf-8")
        bundle = self.root / "patch.zip"
        with (
            mock.patch.object(
                file_sync_module,
                "git_changed_paths",
                return_value=frozenset({PurePosixPath("changed/file.txt")}),
            ),
            mock.patch.object(os, "scandir", wraps=os.scandir) as scandir,
        ):
            pack_sync_bundle(
                source, no_config=True, output_path=bundle, git_changed=True
            )
            apply_sync_bundle(bundle, target, delete=True)
        for root in (source, target):
            self.assertNotIn(mock.call(root / "unrelated"), scandir.call_args_list)
        self.assertEqual((target / "changed" / "file.txt").read_text(), "new content")
        self.assertEqual((target / "unrelated" / "keep.txt").read_text(), "keep")

    def test_uploads_share_confirmed_parent_directory(self) -> None:
        local = self.root / "local"
        source = local / "nested" / "deep"
        source.mkdir(parents=True)
        for index in range(20):
            (source / f"{index}.txt").write_text(str(index), encoding="utf-8")
        remote_store = self.root / "remote"
        (remote_store / "srv" / "nested" / "deep").mkdir(parents=True)
        remote = LocalSFTPRemote(
            build_temporary_sftp_config(local, "server:/srv"), remote_store
        )
        with mock.patch.object(
            remote.adapter, "lstat", wraps=remote.adapter.lstat
        ) as lstat:
            report = sync_workspace_files(
                local,
                SyncDirection.LOCAL_TO_REMOTE,
                remote_endpoint="server:/srv",
                remote_factory=lambda _config: remote,
            )
        self.assertEqual(len(report.actions), 20)
        for index in range(20):
            self.assertEqual(
                (remote_store / "srv" / "nested" / "deep" / f"{index}.txt").read_text(),
                str(index),
            )
        self.assertEqual(lstat.call_args_list.count(mock.call("/srv/nested/deep")), 1)

    def test_remote_directory_cache_survives_type_changes_and_deletion(self) -> None:
        remote_store = self.root / "remote"
        (remote_store / "srv").mkdir(parents=True)
        source = self.root / "folder"
        source.write_text("replacement", encoding="utf-8")
        remote = LocalSFTPRemote(
            build_temporary_sftp_config(self.root, "server:/srv"), remote_store
        )
        folder = PurePosixPath("folder")
        create = SyncAction(ActionKind.CREATE_REMOTE_DIRECTORY, folder)
        create_child = SyncAction(ActionKind.CREATE_REMOTE_DIRECTORY, folder / "child")
        delete = SyncAction(
            ActionKind.DELETE_REMOTE, folder, existing=directory_entry()
        )
        replace_directory = SyncAction(
            ActionKind.COPY_TO_REMOTE,
            folder,
            source=file_entry(),
            existing=directory_entry(),
        )
        with remote:
            remote.apply(create, self.root)
            remote.apply(delete, self.root)
            remote.apply(create, self.root)
            self.assertTrue((remote_store / "srv" / "folder").is_dir())
            remote.apply(create_child, self.root)
            remote.apply(replace_directory, self.root)
            self.assertEqual(
                (remote_store / "srv" / "folder").read_text(), "replacement"
            )
            remote.apply(create, self.root)
            remote.apply(create_child, self.root)
            self.assertTrue((remote_store / "srv" / "folder" / "child").is_dir())

    def test_remote_snapshot_revalidates_previously_confirmed_directories(self) -> None:
        remote_store = self.root / "remote"
        (remote_store / "srv").mkdir(parents=True)
        remote = LocalSFTPRemote(
            build_temporary_sftp_config(self.root, "server:/srv"), remote_store
        )
        create = SyncAction(ActionKind.CREATE_REMOTE_DIRECTORY, PurePosixPath("folder"))
        with remote:
            remote.apply(create, self.root)
            (remote_store / "srv" / "folder").rmdir()
            remote.snapshot(IgnoreMatcher(()))
            remote.apply(create, self.root)
        self.assertTrue((remote_store / "srv" / "folder").is_dir())

    def test_remote_close_discards_confirmed_directories(self) -> None:
        remote_store = self.root / "remote"
        (remote_store / "srv").mkdir(parents=True)
        remote = SFTPRemote(build_temporary_sftp_config(self.root, "server:/srv"))
        adapter = mock.Mock(wraps=LocalSFTPAdapter(remote_store))
        adapter.close = mock.Mock()
        remote._sftp = adapter
        remote.ensure_root()
        remote.__exit__(None, None, None)
        (remote_store / "srv").rmdir()
        remote._sftp = adapter
        try:
            remote.ensure_root()
            self.assertTrue((remote_store / "srv").is_dir())
        finally:
            remote.__exit__(None, None, None)

    def write_config(self, content: str) -> None:
        (self.root / ".vscode" / "sftp.json").write_text(
            textwrap.dedent(content).lstrip(), encoding="utf-8"
        )

    def run_git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd or self.root,
            capture_output=True,
            check=True,
            text=True,
        )
        return result.stdout.strip()

    def initialize_git_repository(self) -> None:
        self.initialize_git_repository_at(self.root)

    def initialize_git_repository_at(self, root: Path) -> None:
        self.run_git("init", "--quiet", cwd=root)
        self.run_git("config", "user.name", "Test User", cwd=root)
        self.run_git("config", "user.email", "test@example.invalid", cwd=root)

    @staticmethod
    def read_bundle_manifest(bundle: Path) -> dict:
        with zipfile.ZipFile(bundle, "r") as archive:
            return json.loads(archive.read(BUNDLE_MANIFEST_PATH))

    @staticmethod
    def rewrite_bundle(
        bundle: Path,
        *,
        manifest: dict | None = None,
        replacements: dict[str, bytes] | None = None,
        extras: dict[str, bytes] | None = None,
    ) -> None:
        replacements = replacements or {}
        extras = extras or {}
        with zipfile.ZipFile(bundle, "r") as archive:
            contents = {
                info.filename: archive.read(info.filename)
                for info in archive.infolist()
            }
        if manifest is not None:
            contents[BUNDLE_MANIFEST_PATH] = (
                json.dumps(manifest, ensure_ascii=False) + "\n"
            ).encode()
        contents.update(replacements)
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in contents.items():
                archive.writestr(name, content)
            for name, content in extras.items():
                archive.writestr(name, content)

    def create_content_project(
        self,
        name: str,
        local_content: bytes,
        remote_content: bytes,
        *,
        local_mtime: float = 200,
        remote_mtime: float = 100,
    ) -> tuple[Path, Path, Path]:
        project = self.root / name
        config_dir = project / ".vscode"
        config_dir.mkdir(parents=True)
        (config_dir / "sftp.json").write_text(
            textwrap.dedent(
                """
                {
                  "name": "content-test",
                  "host": "example.invalid",
                  "remotePath": "/remote",
                  "ignore": [".vscode"]
                }
                """
            ).lstrip(),
            encoding="utf-8",
        )
        local_file = project / "shared.txt"
        local_file.write_bytes(local_content)
        remote_store = self.root / f".{name}-remote"
        remote_file = remote_store / "remote" / "shared.txt"
        remote_file.parent.mkdir(parents=True)
        remote_file.write_bytes(remote_content)
        os.utime(local_file, (local_mtime, local_mtime))
        os.utime(remote_file, (remote_mtime, remote_mtime))
        return project, remote_store, remote_file

    def test_initialize_sftp_config_creates_reference_template(self) -> None:
        project = self.root / "project"
        project.mkdir()
        messages = []

        config_path = initialize_sftp_config(project, progress=messages.append)

        expected = textwrap.dedent(
            """
            {
                "name": "My Server",
                "host": "localhost",
                "protocol": "sftp",
                "port": 22,
                "username": "username",
                "remotePath": "/",
                "uploadOnSave": false,
                "useTempFile": false,
                "openSsh": false
            }
            """
        ).lstrip()
        self.assertEqual(config_path, project / ".vscode" / "sftp.json")
        self.assertEqual(config_path.read_text(encoding="utf-8"), expected)
        self.assertEqual(render_default_sftp_config(), expected)
        self.assertEqual(messages, [f"initialized SFTP config: {config_path}"])
        loaded = load_sftp_config(project)
        self.assertEqual(
            (loaded.host, loaded.username, loaded.remote_root),
            ("localhost", "username", "/"),
        )
        self.assertFalse(
            any(
                path.name.startswith(".sftp.json.hgc-")
                for path in config_path.parent.iterdir()
            )
        )

    def test_initialize_sftp_config_dry_run_does_not_create_files(self) -> None:
        project = self.root / "dry-run-project"
        project.mkdir()
        messages = []

        config_path = initialize_sftp_config(
            project, dry_run=True, progress=messages.append
        )

        self.assertEqual(config_path, project / ".vscode" / "sftp.json")
        self.assertFalse((project / ".vscode").exists())
        self.assertEqual(messages[0], f"Would create SFTP config: {config_path}")
        self.assertEqual(messages[1] + "\n", render_default_sftp_config())

    def test_initialize_sftp_config_refuses_existing_unless_forced(self) -> None:
        config_path = self.root / ".vscode" / "sftp.json"
        original = b'{"name":"keep"}\n'
        config_path.write_bytes(original)

        with self.assertRaisesRegex(FileSyncConfigError, "already exists"):
            initialize_sftp_config(self.root)
        self.assertEqual(config_path.read_bytes(), original)

        messages = []
        initialize_sftp_config(
            self.root,
            force=True,
            dry_run=True,
            progress=messages.append,
        )
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(messages[0], f"Would overwrite SFTP config: {config_path}")

        initialize_sftp_config(self.root, force=True)
        self.assertEqual(
            config_path.read_text(encoding="utf-8"), render_default_sftp_config()
        )

    def test_initialize_sftp_config_keeps_existing_file_on_atomic_write_failure(
        self,
    ) -> None:
        config_path = self.root / ".vscode" / "sftp.json"
        original = b'{"name":"keep"}\n'
        config_path.write_bytes(original)

        with mock.patch(
            "hagency_cli.file_sync.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaisesRegex(FileSyncError, "cannot write SFTP config"):
                initialize_sftp_config(self.root, force=True)

        self.assertEqual(config_path.read_bytes(), original)
        self.assertFalse(
            any(
                path.name.startswith(".sftp.json.hgc-")
                for path in config_path.parent.iterdir()
            )
        )

    def test_initialize_sftp_config_validates_target_paths(self) -> None:
        missing = self.root / "missing"
        with self.assertRaisesRegex(FileSyncConfigError, "does not exist"):
            initialize_sftp_config(missing)

        project_file = self.root / "project-file"
        project_file.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(FileSyncConfigError, "not a directory"):
            initialize_sftp_config(project_file)

        blocked = self.root / "blocked"
        blocked.mkdir()
        (blocked / ".vscode").write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(FileSyncConfigError, "config directory"):
            initialize_sftp_config(blocked)

        config_directory = self.root / "config-directory"
        (config_directory / ".vscode" / "sftp.json").mkdir(parents=True)
        with self.assertRaisesRegex(FileSyncConfigError, "config path is not a file"):
            initialize_sftp_config(config_directory, force=True)

    def test_config_is_discovered_in_selected_directory_and_normalizes_paths(
        self,
    ) -> None:
        (self.root / "src").mkdir()
        (self.root / ".syncignore").write_text("*.tmp\n", encoding="utf-8")
        self.write_config(
            """
            {
              "name": "win",
              "host": "desktop",
              "port": 2222,
              "username": "kevin",
              "protocol": "sftp",
              "context": "src",
              "remotePath": "D:\\\\Projects\\\\ws",
              "privateKeyPath": "~/.ssh/id_ed25519",
              "ignore": [".git"],
              "ignoreFile": ".syncignore",
              "filePerm": 644,
              "dirPerm": 750,
              "syncOption": {"delete": true, "update": true}
            }
            """
        )

        config = load_sftp_config(self.root)

        self.assertEqual(config.config_path, self.root / ".vscode" / "sftp.json")
        self.assertEqual(config.local_root, self.root / "src")
        self.assertEqual(config.remote_root, "D:/Projects/ws")
        self.assertEqual(config.endpoint, "kevin@desktop:2222:D:/Projects/ws")
        self.assertEqual(
            config.ignore_patterns,
            (".git", "*.tmp", "/.vscode/sftp.json"),
        )
        self.assertEqual(config.file_perm, 0o644)
        self.assertEqual(config.dir_perm, 0o750)
        self.assertTrue(config.sync_options.delete)
        self.assertTrue(config.sync_options.update)

    def test_missing_current_directory_config_is_reported(self) -> None:
        with self.assertRaisesRegex(FileSyncConfigError, "missing config"):
            load_sftp_config(self.root / "elsewhere")

    def test_temporary_endpoint_parser_supports_scp_path_forms(self) -> None:
        cases = {
            "server:.": RemoteEndpoint("server", "."),
            "dev@server:~/Projects/ws": RemoteEndpoint("server", "Projects/ws", "dev"),
            "server:C:/Windows/path": RemoteEndpoint("server", "C:/Windows/path"),
            "[2001:db8::1]:/srv/ws": RemoteEndpoint("2001:db8::1", "/srv/ws"),
            "dev@[fe80::1%en0]:~/ws": RemoteEndpoint("fe80::1%en0", "ws", "dev"),
        }

        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_remote_endpoint(value), expected)

    def test_temporary_endpoint_parser_rejects_ambiguous_or_empty_values(
        self,
    ) -> None:
        invalid = (
            "",
            "server",
            "server:",
            "@server:/srv",
            "dev@:/srv",
            "dev@@server:/srv",
            "2001:db8::1:/srv",
            "[server]:/srv",
            "server:~",
            "server:~//srv",
            "server:~other/ws",
            " server:/srv",
            "server:/srv\nother",
        )

        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(FileSyncConfigError):
                    parse_remote_endpoint(value)

    def test_temporary_config_uses_safe_defaults_and_protected_excludes(
        self,
    ) -> None:
        identity = self.root / "id_ed25519"
        config = build_temporary_sftp_config(
            self.root,
            "dev@[2001:db8::1]:~/Projects/ws",
            port=2222,
            identity=identity,
            exclude=("*.tmp", "!.git/config", "!/.vscode/sftp.json"),
            sync_options=SyncOptions(skip_create=True, update=True),
        )

        self.assertIsNone(config.config_path)
        self.assertEqual(config.source, "temporary endpoint")
        self.assertEqual(config.local_root, self.root)
        self.assertEqual(config.remote_root, "Projects/ws")
        self.assertEqual(config.host, "2001:db8::1")
        self.assertEqual(config.username, "dev")
        self.assertEqual(config.port, 2222)
        self.assertEqual(config.private_key_path, identity)
        self.assertEqual(config.endpoint, "dev@[2001:db8::1]:2222:Projects/ws")
        self.assertEqual(config.ignore_patterns[-2:], (".git/", "/.vscode/sftp.json"))
        self.assertFalse(config.sync_options.delete)
        self.assertTrue(config.sync_options.skip_create)
        self.assertTrue(config.sync_options.update)

        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / ".vscode" / "sftp.json").write_text("secret", encoding="utf-8")
        (self.root / "keep.txt").write_text("keep", encoding="utf-8")
        result = scan_local(
            config.local_root,
            IgnoreMatcher(config.ignore_patterns, config.protected_paths),
        )
        self.assertEqual(
            set(result.entries),
            {PurePosixPath(".vscode"), PurePosixPath("keep.txt")},
        )

    def test_temporary_config_rejects_invalid_port(self) -> None:
        for port in (0, 65536, True):
            with self.subTest(port=port):
                with self.assertRaisesRegex(FileSyncConfigError, "port"):
                    build_temporary_sftp_config(self.root, "server:/srv", port=port)

    def test_default_and_explicit_profiles_merge_base_config(self) -> None:
        self.write_config(
            """
            {
              "name": "server",
              "host": "base.example",
              "username": "dev",
              "remotePath": "/base",
              "ignore": [".git"],
              "defaultProfile": "staging",
              "profiles": {
                "staging": {
                  "host": "staging.example",
                  "remotePath": "/staging",
                  "ignore": ["*.log"]
                },
                "prod": {"host": "prod.example", "remotePath": "/prod"}
              }
            }
            """
        )

        default = load_sftp_config(self.root)
        production = load_sftp_config(self.root, "prod")
        base = load_sftp_config(self.root, "server:")

        self.assertEqual(default.selection, "server:staging")
        self.assertEqual(default.host, "staging.example")
        self.assertEqual(
            default.ignore_patterns,
            (".git", "*.log", "/.vscode/sftp.json"),
        )
        self.assertEqual(production.selection, "server:prod")
        self.assertEqual(production.host, "prod.example")
        self.assertEqual(base.selection, "server")
        self.assertEqual(base.host, "base.example")

    def test_ambiguous_short_profile_requires_an_explicit_selector(self) -> None:
        self.write_config(
            """
            [
              {"name": "prod", "host": "direct", "remotePath": "/direct"},
              {
                "name": "server",
                "host": "base",
                "remotePath": "/base",
                "profiles": {
                  "prod": {"host": "nested", "remotePath": "/nested"}
                }
              }
            ]
            """
        )

        with self.assertRaisesRegex(FileSyncConfigError, "ambiguous SFTP profile"):
            load_sftp_config(self.root, "prod")

        self.assertEqual(load_sftp_config(self.root, "prod:").host, "direct")
        self.assertEqual(load_sftp_config(self.root, "server:prod").host, "nested")

    def test_multiple_configs_require_a_named_selection(self) -> None:
        self.write_config(
            """
            [
              {"name": "one", "host": "one", "username": "u", "remotePath": "/one"},
              {"name": "two", "host": "two", "username": "u", "remotePath": "/two"}
            ]
            """
        )
        with self.assertRaisesRegex(FileSyncConfigError, "multiple SFTP configs"):
            load_sftp_config(self.root)

        selected = load_sftp_config(self.root, "two")
        self.assertEqual(selected.host, "two")

    def test_non_sftp_protocol_and_invalid_atomic_upload_are_rejected(self) -> None:
        self.write_config(
            '{"name":"ftp","protocol":"ftp","host":"host","username":"u"}'
        )
        with self.assertRaisesRegex(FileSyncConfigError, "currently supports SFTP"):
            load_sftp_config(self.root)

        self.write_config(
            '{"name":"bad","host":"host","username":"u",'
            '"remotePath":"/x","openSsh":true}'
        )
        with self.assertRaisesRegex(
            FileSyncConfigError, "openSsh requires useTempFile"
        ):
            load_sftp_config(self.root)

    def test_zero_permissions_are_valid(self) -> None:
        self.write_config(
            '{"name":"locked","host":"host","remotePath":"/x","filePerm":0,"dirPerm":0}'
        )

        config = load_sftp_config(self.root)

        self.assertEqual(config.file_perm, 0)
        self.assertEqual(config.dir_perm, 0)

    def test_config_protection_survives_negation_and_ignore_file_bom(self) -> None:
        (self.root / "secret.txt").write_text("secret", encoding="utf-8")
        (self.root / ".vscode" / "SFTP.JSON").write_text(
            "remote credentials", encoding="utf-8"
        )
        (self.root / ".syncignore").write_text(
            "\ufeffsecret.txt\n!/.vscode/sftp.json\n", encoding="utf-8"
        )
        self.write_config(
            """
            {
              "name": "safe",
              "host": "host",
              "remotePath": "/x",
              "ignore": ["!/.vscode/sftp.json"],
              "ignoreFile": ".syncignore"
            }
            """
        )
        config = load_sftp_config(self.root)

        result = scan_local(
            config.local_root,
            IgnoreMatcher(config.ignore_patterns, config.protected_paths),
        )

        self.assertNotIn(PurePosixPath("secret.txt"), result.entries)
        self.assertNotIn(PurePosixPath(".vscode/sftp.json"), result.entries)
        self.assertNotIn(PurePosixPath(".vscode/SFTP.JSON"), result.entries)

    def test_config_inside_context_is_also_protected(self) -> None:
        self.write_config(
            """
            {
              "name": "safe-context",
              "host": "host",
              "context": ".vscode",
              "remotePath": "/x",
              "ignore": ["!/sftp.json"]
            }
            """
        )
        config = load_sftp_config(self.root)

        result = scan_local(
            config.local_root,
            IgnoreMatcher(config.ignore_patterns, config.protected_paths),
        )

        self.assertNotIn(PurePosixPath("sftp.json"), result.entries)

    def test_local_scanner_uses_gitignore_rules_and_does_not_follow_symlinks(
        self,
    ) -> None:
        (self.root / "keep").mkdir()
        (self.root / "keep" / "a.txt").write_text("a", encoding="utf-8")
        (self.root / "keep" / "drop.tmp").write_text("x", encoding="utf-8")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("x", encoding="utf-8")
        os.symlink("keep/a.txt", self.root / "link")

        result = scan_local(self.root, IgnoreMatcher((".git", "*.tmp", ".vscode")))

        self.assertEqual(
            set(result.entries),
            {PurePosixPath("keep"), PurePosixPath("keep/a.txt"), PurePosixPath("link")},
        )
        self.assertEqual(result.entries[PurePosixPath("link")].kind, EntryKind.SYMLINK)
        self.assertEqual(
            result.entries[PurePosixPath("link")].link_target, "keep/a.txt"
        )

    def test_local_scanner_keeps_negated_child_when_parent_is_traversable(
        self,
    ) -> None:
        (self.root / "build").mkdir()
        (self.root / "build" / "drop.txt").write_text("drop", encoding="utf-8")
        (self.root / "build" / "keep.txt").write_text("keep", encoding="utf-8")

        result = scan_local(
            self.root,
            IgnoreMatcher(("build/*", "!build/keep.txt", ".vscode")),
        )

        self.assertIn(PurePosixPath("build"), result.entries)
        self.assertIn(PurePosixPath("build/keep.txt"), result.entries)
        self.assertNotIn(PurePosixPath("build/drop.txt"), result.entries)

    def test_local_to_remote_plan_copies_changes_creates_and_deletes(self) -> None:
        local = snapshot(
            {
                "shared.txt": file_entry(mtime=20, size=2),
                "new.txt": file_entry(mtime=10),
                "folder": directory_entry(),
                "folder/child.txt": file_entry(mtime=10),
            }
        )
        remote = snapshot(
            {
                "shared.txt": file_entry(mtime=10, size=1),
                "old.txt": file_entry(mtime=10),
            }
        )

        plan = build_sync_plan(
            SyncDirection.LOCAL_TO_REMOTE,
            local,
            remote,
            SyncOptions(delete=True),
        )

        self.assertEqual(plan[0].kind, ActionKind.DELETE_REMOTE)
        self.assertEqual(plan[0].path, PurePosixPath("old.txt"))
        self.assertIn(
            (ActionKind.CREATE_REMOTE_DIRECTORY, PurePosixPath("folder")),
            [(action.kind, action.path) for action in plan],
        )
        uploads = {
            action.path for action in plan if action.kind is ActionKind.COPY_TO_REMOTE
        }
        self.assertEqual(
            uploads,
            {
                PurePosixPath("shared.txt"),
                PurePosixPath("new.txt"),
                PurePosixPath("folder/child.txt"),
            },
        )

    def test_remote_to_local_respects_update_skip_create_and_ignore_existing(
        self,
    ) -> None:
        local = snapshot(
            {
                "older-local.txt": file_entry(mtime=5),
                "newer-local.txt": file_entry(mtime=30),
                "ignored.txt": file_entry(mtime=1),
            }
        )
        remote = snapshot(
            {
                "older-local.txt": file_entry(mtime=20),
                "newer-local.txt": file_entry(mtime=20),
                "ignored.txt": file_entry(mtime=50),
                "remote-only.txt": file_entry(mtime=10),
            }
        )

        plan = build_sync_plan(
            SyncDirection.REMOTE_TO_LOCAL,
            local,
            remote,
            SyncOptions(update=True, skip_create=True),
        )
        self.assertEqual(
            [(action.kind, action.path) for action in plan],
            [
                (ActionKind.COPY_TO_LOCAL, PurePosixPath("ignored.txt")),
                (ActionKind.COPY_TO_LOCAL, PurePosixPath("older-local.txt")),
            ],
        )

        ignored = build_sync_plan(
            SyncDirection.REMOTE_TO_LOCAL,
            local,
            remote,
            SyncOptions(ignore_existing=True, skip_create=True),
        )
        self.assertEqual(ignored, [])

    def test_both_directions_uses_newest_file_and_copies_unique_files(self) -> None:
        local = snapshot(
            {
                "local-newer.txt": file_entry(mtime=30),
                "remote-newer.txt": file_entry(mtime=10),
                "local-only.txt": file_entry(mtime=10),
            }
        )
        remote = snapshot(
            {
                "local-newer.txt": file_entry(mtime=20),
                "remote-newer.txt": file_entry(mtime=40),
                "remote-only.txt": file_entry(mtime=10),
            }
        )

        plan = build_sync_plan(SyncDirection.BOTH, local, remote, SyncOptions())
        operations = {(action.kind, action.path) for action in plan}

        self.assertEqual(
            operations,
            {
                (ActionKind.COPY_TO_REMOTE, PurePosixPath("local-newer.txt")),
                (ActionKind.COPY_TO_LOCAL, PurePosixPath("remote-newer.txt")),
                (ActionKind.COPY_TO_REMOTE, PurePosixPath("local-only.txt")),
                (ActionKind.COPY_TO_LOCAL, PurePosixPath("remote-only.txt")),
            },
        )

    def test_type_conflict_uses_winning_tree_without_reintroducing_losing_children(
        self,
    ) -> None:
        local = snapshot({"node": file_entry(mtime=30)})
        remote = snapshot(
            {
                "node": directory_entry(mtime=10),
                "node/remote-child.txt": file_entry(mtime=10),
            }
        )

        plan = build_sync_plan(SyncDirection.BOTH, local, remote, SyncOptions())

        self.assertEqual(
            [(action.kind, action.path) for action in plan],
            [(ActionKind.COPY_TO_REMOTE, PurePosixPath("node"))],
        )

    def test_matching_symlink_targets_do_not_repeat_only_for_timestamp_drift(
        self,
    ) -> None:
        local_link = FileEntry(
            EntryKind.SYMLINK,
            size=6,
            mtime=10,
            mode=0o777,
            link_target="target",
        )
        remote_link = FileEntry(
            EntryKind.SYMLINK,
            size=6,
            mtime=20,
            mode=0o777,
            link_target="target",
        )

        plan = build_sync_plan(
            SyncDirection.BOTH,
            snapshot({"link": local_link}),
            snapshot({"link": remote_link}),
            SyncOptions(),
        )

        self.assertEqual(plan, [])

    def test_dry_run_scans_remote_but_does_not_apply_actions(self) -> None:
        (self.root / "local.txt").write_text("local", encoding="utf-8")
        self.write_config(
            """
            {
              "name": "test",
              "host": "example.invalid",
              "username": "u",
              "remotePath": "/remote",
              "ignore": [".vscode"]
            }
            """
        )
        fake = FakeRemote(snapshot({}))
        messages = []

        report = sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            dry_run=True,
            progress=messages.append,
            remote_factory=lambda _config: fake,
        )

        self.assertEqual(fake.ensure_root_calls, 0)
        self.assertEqual(fake.applied, [])
        self.assertEqual(len(report.actions), 1)
        self.assertIn("would upload: local.txt", messages)

    def test_real_run_ensures_destination_root_and_applies_plan(self) -> None:
        (self.root / "local.txt").write_text("local", encoding="utf-8")
        self.write_config(
            """
            {
              "name": "test",
              "host": "example.invalid",
              "username": "u",
              "remotePath": "/remote",
              "ignore": [".vscode"]
            }
            """
        )
        fake = FakeRemote(snapshot({}, exists=False))

        report = sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            remote_factory=lambda _config: fake,
        )

        self.assertEqual(fake.ensure_root_calls, 1)
        self.assertEqual(len(fake.applied), 2)
        self.assertEqual(
            [item[0].kind for item in fake.applied],
            [ActionKind.CREATE_REMOTE_DIRECTORY, ActionKind.COPY_TO_REMOTE],
        )
        self.assertEqual(len(report.actions), 2)

    def test_bidirectional_sync_applies_real_file_transfers(self) -> None:
        remote_store = self.root / ".remote-store"
        remote_root = remote_store / "remote"
        remote_root.mkdir(parents=True)
        local_newer = self.root / "local-newer.txt"
        local_newer.write_text("from local", encoding="utf-8")
        remote_older = remote_root / "local-newer.txt"
        remote_older.write_text("old", encoding="utf-8")
        remote_only = remote_root / "remote-only.txt"
        remote_only.write_text("from remote", encoding="utf-8")
        os.utime(local_newer, (200, 200))
        os.utime(remote_older, (100, 100))
        os.utime(remote_only, (300, 300))
        self.write_config(
            """
            {
              "name": "local-adapter",
              "host": "example.invalid",
              "username": "u",
              "remotePath": "/remote",
              "ignore": [".vscode", ".remote-store"]
            }
            """
        )

        report = sync_workspace_files(
            self.root,
            SyncDirection.BOTH,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(remote_older.read_text(encoding="utf-8"), "from local")
        self.assertEqual(
            (self.root / "remote-only.txt").read_text(encoding="utf-8"),
            "from remote",
        )
        self.assertEqual(
            {(action.kind, action.path) for action in report.actions},
            {
                (ActionKind.COPY_TO_REMOTE, PurePosixPath("local-newer.txt")),
                (ActionKind.COPY_TO_LOCAL, PurePosixPath("remote-only.txt")),
            },
        )

    def test_temporary_endpoint_bypasses_invalid_project_config(self) -> None:
        project = self.root / "temporary-project"
        (project / ".vscode").mkdir(parents=True)
        (project / ".vscode" / "sftp.json").write_text(
            "not valid json", encoding="utf-8"
        )
        (project / ".git").mkdir()
        (project / ".git" / "config").write_text("private", encoding="utf-8")
        (project / "local.txt").write_text("from local", encoding="utf-8")
        remote_store = self.root / "temporary-remote"
        remote_store.mkdir()
        messages: list[str] = []

        report = sync_workspace_files(
            project,
            SyncDirection.LOCAL_TO_REMOTE,
            remote_endpoint="server:/remote",
            progress=messages.append,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertIsNone(report.config.config_path)
        self.assertIn("config: temporary endpoint", messages)
        self.assertEqual(
            (remote_store / "remote" / "local.txt").read_text(encoding="utf-8"),
            "from local",
        )
        self.assertFalse((remote_store / "remote" / ".git").exists())
        self.assertFalse((remote_store / "remote" / ".vscode" / "sftp.json").exists())

    def test_temporary_endpoint_supports_download_and_bidirectional_sync(
        self,
    ) -> None:
        download_root = self.root / "download"
        download_store = self.root / "download-store"
        remote_download = download_store / "remote"
        remote_download.mkdir(parents=True)
        (remote_download / "remote.txt").write_text("remote", encoding="utf-8")

        download_report = sync_workspace_files(
            download_root,
            SyncDirection.REMOTE_TO_LOCAL,
            remote_endpoint="server:/remote",
            remote_factory=lambda config: LocalSFTPRemote(config, download_store),
        )

        self.assertEqual(
            (download_root / "remote.txt").read_text(encoding="utf-8"), "remote"
        )
        self.assertIn(
            (ActionKind.COPY_TO_LOCAL, PurePosixPath("remote.txt")),
            {(action.kind, action.path) for action in download_report.actions},
        )

        both_root = self.root / "both-root"
        both_root.mkdir()
        (both_root / "local.txt").write_text("local", encoding="utf-8")
        both_store = self.root / "both-store"
        remote_both = both_store / "remote"
        remote_both.mkdir(parents=True)
        (remote_both / "remote.txt").write_text("remote", encoding="utf-8")

        sync_workspace_files(
            both_root,
            SyncDirection.BOTH,
            remote_endpoint="server:/remote",
            remote_factory=lambda config: LocalSFTPRemote(config, both_store),
        )

        self.assertEqual(
            (remote_both / "local.txt").read_text(encoding="utf-8"), "local"
        )
        self.assertEqual(
            (both_root / "remote.txt").read_text(encoding="utf-8"), "remote"
        )

    def test_temporary_options_require_endpoint_and_profile_is_mutually_exclusive(
        self,
    ) -> None:
        remote_factory = mock.Mock()
        cases = (
            {"port": 2222},
            {"identity": self.root / "id"},
            {"exclude": ("*.tmp",)},
            {"delete": True},
            {"skip_create": True},
            {"ignore_existing": True},
            {"update": True},
        )
        for options in cases:
            with self.subTest(options=options):
                with self.assertRaisesRegex(FileSyncConfigError, "require a remote"):
                    sync_workspace_files(
                        self.root,
                        SyncDirection.LOCAL_TO_REMOTE,
                        remote_factory=remote_factory,
                        **options,
                    )

        with self.assertRaisesRegex(FileSyncConfigError, "mutually exclusive"):
            sync_workspace_files(
                self.root,
                SyncDirection.LOCAL_TO_REMOTE,
                profile="prod",
                remote_endpoint="server:/remote",
                remote_factory=remote_factory,
            )
        with self.assertRaisesRegex(FileSyncConfigError, "bidirectional"):
            sync_workspace_files(
                self.root,
                SyncDirection.BOTH,
                remote_endpoint="server:/remote",
                delete=True,
                remote_factory=remote_factory,
            )
        remote_factory.assert_not_called()

    def test_temporary_delete_cannot_cross_builtin_excludes(self) -> None:
        project = self.root / "delete-project"
        project.mkdir()
        (project / "keep.txt").write_text("keep", encoding="utf-8")
        remote_store = self.root / "delete-store"
        remote_root = remote_store / "remote"
        (remote_root / ".git").mkdir(parents=True)
        (remote_root / ".git" / "config").write_text("private", encoding="utf-8")
        (remote_root / ".vscode").mkdir()
        (remote_root / ".vscode" / "sftp.json").write_text("private", encoding="utf-8")
        (remote_root / "old.txt").write_text("old", encoding="utf-8")

        sync_workspace_files(
            project,
            SyncDirection.LOCAL_TO_REMOTE,
            remote_endpoint="server:/remote",
            exclude=("!.git/config", "!/.vscode/sftp.json"),
            delete=True,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertFalse((remote_root / "old.txt").exists())
        self.assertTrue((remote_root / ".git" / "config").is_file())
        self.assertTrue((remote_root / ".vscode" / "sftp.json").is_file())

    def test_temp_upload_does_not_overwrite_real_dot_new_file(self) -> None:
        remote_store = self.root / ".remote-store"
        remote_root = remote_store / "remote"
        remote_root.mkdir(parents=True)
        local_file = self.root / "a"
        local_file.write_text("new a", encoding="utf-8")
        remote_file = remote_root / "a"
        remote_file.write_text("old a", encoding="utf-8")
        remote_dot_new = remote_root / "a.new"
        remote_dot_new.write_text("keep me", encoding="utf-8")
        os.utime(local_file, (300, 300))
        os.utime(remote_file, (100, 100))
        os.utime(remote_dot_new, (200, 200))
        self.write_config(
            """
            {
              "name": "temp-upload",
              "host": "example.invalid",
              "remotePath": "/remote",
              "ignore": [".vscode", ".remote-store"],
              "useTempFile": true
            }
            """
        )

        sync_workspace_files(
            self.root,
            SyncDirection.BOTH,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(remote_file.read_text(encoding="utf-8"), "new a")
        self.assertEqual(remote_dot_new.read_text(encoding="utf-8"), "keep me")
        self.assertEqual((self.root / "a.new").read_text(encoding="utf-8"), "keep me")
        self.assertFalse(
            any(path.name.startswith(".hgc-upload-") for path in remote_root.iterdir())
        )

    def test_new_remote_parent_directories_receive_dir_permission(self) -> None:
        remote_store = self.root / ".remote-store"
        remote_store.mkdir()
        (self.root / "local.txt").write_text("local", encoding="utf-8")
        self.write_config(
            """
            {
              "name": "directory-mode",
              "host": "example.invalid",
              "remotePath": "/ancestor/remote",
              "ignore": [".vscode", ".remote-store"],
              "dirPerm": 750
            }
            """
        )

        sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(
            stat.S_IMODE((remote_store / "ancestor").stat().st_mode), 0o750
        )
        self.assertEqual(
            stat.S_IMODE((remote_store / "ancestor" / "remote").stat().st_mode),
            0o750,
        )

    def test_remote_directory_creation_omits_none_mode(self) -> None:
        self.write_config('{"name":"default-mode","host":"host","remotePath":"/new"}')
        remote = SFTPRemote(load_sftp_config(self.root))
        adapter = mock.Mock()

        def lstat(path):
            if path == "/":
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
            raise FileNotFoundError(path)

        adapter.lstat.side_effect = lstat
        remote._sftp = adapter

        remote._ensure_directory("/new")

        adapter.mkdir.assert_called_once_with("/new")

    def test_temporary_connection_honors_ssh_config_and_cli_precedence(
        self,
    ) -> None:
        ssh_config_path = self.root / "ssh-config"
        config_identity = self.root / "from-config"
        cli_identity = self.root / "from-cli"
        ssh_config_path.write_text(
            textwrap.dedent(
                f"""
                Host win-wsl
                  HostName wsl.example.invalid
                  User config-user
                  Port 2222
                  IdentityFile {config_identity}
                  ProxyCommand proxy-tool %h %p %r
                  RequestTTY yes
                  RemoteCommand wsl bash -lc true
                """
            ).lstrip(),
            encoding="utf-8",
        )

        for endpoint, port, identity, expected in (
            (
                "win-wsl:/home/config-user/ws",
                None,
                None,
                ("wsl.example.invalid", 2222, "config-user", [str(config_identity)]),
            ),
            (
                "cli-user@win-wsl:/home/cli-user/ws",
                2200,
                cli_identity,
                ("wsl.example.invalid", 2200, "cli-user", str(cli_identity)),
            ),
        ):
            with self.subTest(endpoint=endpoint):
                config = replace(
                    build_temporary_sftp_config(
                        self.root,
                        endpoint,
                        port=port,
                        identity=identity,
                    ),
                    ssh_config_path=ssh_config_path,
                )
                client = mock.Mock()
                proxy = mock.Mock()
                with (
                    mock.patch("paramiko.SSHClient", return_value=client),
                    mock.patch(
                        "paramiko.ProxyCommand", return_value=proxy
                    ) as proxy_command,
                ):
                    with SFTPRemote(config):
                        pass

                connect_options = client.connect.call_args.kwargs
                self.assertEqual(
                    (
                        connect_options["hostname"],
                        connect_options["port"],
                        connect_options["username"],
                        connect_options["key_filename"],
                    ),
                    expected,
                )
                self.assertNotIn("remotecommand", connect_options)
                self.assertNotIn("requesttty", connect_options)
                proxy_command.assert_called_once_with(
                    "proxy-tool wsl.example.invalid "
                    + str(expected[1])
                    + " "
                    + str(expected[2])
                )

    def test_temporary_connection_falls_back_to_current_system_user(self) -> None:
        config = replace(
            build_temporary_sftp_config(self.root, "server:/srv"),
            ssh_config_path=self.root / "missing-ssh-config",
        )
        client = mock.Mock()
        with (
            mock.patch("paramiko.SSHClient", return_value=client),
            mock.patch(
                "hagency_cli.file_sync.getpass.getuser", return_value="local-user"
            ),
        ):
            with SFTPRemote(config):
                pass

        self.assertEqual(client.connect.call_args.kwargs["username"], "local-user")
        self.assertEqual(client.connect.call_args.kwargs["port"], 22)

    def test_remote_snapshot_rejects_parent_directory_entries(self) -> None:
        self.write_config('{"name":"unsafe","host":"host","remotePath":"/x"}')
        remote = SFTPRemote(load_sftp_config(self.root))
        adapter = mock.Mock()
        adapter.lstat.return_value = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
        adapter.listdir_attr.return_value = [
            SimpleNamespace(
                filename="..",
                st_mode=stat.S_IFREG | 0o644,
                st_size=1,
                st_mtime=1,
            )
        ]
        remote._sftp = adapter

        with self.assertRaisesRegex(FileSyncError, "unsafe remote directory entry"):
            remote.snapshot(IgnoreMatcher(()))

    @unittest.skipUnless(shutil.which("git"), "Git is required for this test")
    def test_git_changed_paths_include_all_worktree_states_and_exclude_ignored(
        self,
    ) -> None:
        self.initialize_git_repository()
        (self.root / ".gitignore").write_text(
            ".vscode/\nignored.txt\n", encoding="utf-8"
        )
        for name in (
            "staged.txt",
            "unstaged.txt",
            "deleted.txt",
            "renamed-old.txt",
            "clean.txt",
        ):
            (self.root / name).write_text("original", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "initial")

        (self.root / "staged.txt").write_text("staged", encoding="utf-8")
        self.run_git("add", "staged.txt")
        (self.root / "unstaged.txt").write_text("unstaged", encoding="utf-8")
        (self.root / "untracked.txt").write_text("untracked", encoding="utf-8")
        (self.root / "deleted.txt").unlink()
        self.run_git("mv", "renamed-old.txt", "renamed-new.txt")
        (self.root / "ignored.txt").write_text("ignored", encoding="utf-8")

        self.assertEqual(
            git_changed_paths(self.root),
            frozenset(
                {
                    PurePosixPath("staged.txt"),
                    PurePosixPath("unstaged.txt"),
                    PurePosixPath("untracked.txt"),
                    PurePosixPath("deleted.txt"),
                    PurePosixPath("renamed-old.txt"),
                    PurePosixPath("renamed-new.txt"),
                }
            ),
        )

    @unittest.skipUnless(shutil.which("git"), "Git is required for this test")
    def test_git_changed_paths_are_relative_to_nested_context(self) -> None:
        self.initialize_git_repository()
        context = self.root / "app"
        context.mkdir()
        (context / "inside.txt").write_text("original", encoding="utf-8")
        (self.root / "outside.txt").write_text("original", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "initial")

        (context / "inside.txt").write_text("changed", encoding="utf-8")
        (self.root / "outside.txt").write_text("changed", encoding="utf-8")

        self.assertEqual(
            git_changed_paths(context),
            frozenset({PurePosixPath("inside.txt")}),
        )

    @unittest.skipUnless(shutil.which("git"), "Git is required for this test")
    def test_git_changed_sync_filters_uploads_and_deletes(self) -> None:
        self.write_config(
            """
            {
              "name": "git-filter",
              "host": "example.invalid",
              "remotePath": "/remote",
              "ignore": [".git", ".vscode", ".remote-store"],
              "syncOption": {"delete": true}
            }
            """
        )
        self.initialize_git_repository()
        (self.root / ".gitignore").write_text(
            ".vscode/\n.remote-store/\n", encoding="utf-8"
        )
        for name in ("changed.txt", "deleted.txt", "unchanged.txt"):
            (self.root / name).write_text(f"local {name}", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "initial")

        (self.root / "changed.txt").write_text("new local value", encoding="utf-8")
        (self.root / "deleted.txt").unlink()
        (self.root / "new.txt").write_text("new file", encoding="utf-8")
        remote_store = self.root / ".remote-store"
        remote_root = remote_store / "remote"
        remote_root.mkdir(parents=True)
        (remote_root / "changed.txt").write_text("old remote", encoding="utf-8")
        (remote_root / "deleted.txt").write_text("delete me", encoding="utf-8")
        (remote_root / "unchanged.txt").write_text(
            "remote drift must stay", encoding="utf-8"
        )
        (remote_root / "remote-only.txt").write_text("keep me", encoding="utf-8")

        report = sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            git_changed=True,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(
            {(action.kind, action.path) for action in report.actions},
            {
                (ActionKind.COPY_TO_REMOTE, PurePosixPath("changed.txt")),
                (ActionKind.COPY_TO_REMOTE, PurePosixPath("new.txt")),
                (ActionKind.DELETE_REMOTE, PurePosixPath("deleted.txt")),
            },
        )
        self.assertEqual(
            (remote_root / "changed.txt").read_text(encoding="utf-8"),
            "new local value",
        )
        self.assertEqual(
            (remote_root / "new.txt").read_text(encoding="utf-8"), "new file"
        )
        self.assertFalse((remote_root / "deleted.txt").exists())
        self.assertEqual(
            (remote_root / "unchanged.txt").read_text(encoding="utf-8"),
            "remote drift must stay",
        )
        self.assertEqual(
            (remote_root / "remote-only.txt").read_text(encoding="utf-8"),
            "keep me",
        )

    @unittest.skipUnless(shutil.which("git"), "Git is required for this test")
    def test_git_rename_keeps_old_remote_path_without_delete_option(self) -> None:
        self.write_config(
            """
            {
              "name": "git-rename",
              "host": "example.invalid",
              "remotePath": "/remote",
              "ignore": [".git", ".vscode", ".remote-store"]
            }
            """
        )
        self.initialize_git_repository()
        (self.root / ".gitignore").write_text(
            ".vscode/\n.remote-store/\n", encoding="utf-8"
        )
        (self.root / "old.txt").write_text("renamed content", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "initial")
        self.run_git("mv", "old.txt", "new.txt")
        remote_store = self.root / ".remote-store"
        remote_file = remote_store / "remote" / "old.txt"
        remote_file.parent.mkdir(parents=True)
        remote_file.write_text("renamed content", encoding="utf-8")

        report = sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            git_changed=True,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(
            [(action.kind, action.path) for action in report.actions],
            [(ActionKind.COPY_TO_REMOTE, PurePosixPath("new.txt"))],
        )
        self.assertTrue(remote_file.is_file())
        self.assertEqual(
            (remote_store / "remote" / "new.txt").read_text(encoding="utf-8"),
            "renamed content",
        )

    @unittest.skipUnless(shutil.which("git"), "Git is required for this test")
    def test_clean_git_filter_returns_without_connecting(self) -> None:
        self.write_config('{"name":"clean","host":"host","ignore":[".git",".vscode"]}')
        self.initialize_git_repository()
        (self.root / ".gitignore").write_text(".vscode/\n", encoding="utf-8")
        (self.root / "tracked.txt").write_text("clean", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "--quiet", "-m", "initial")
        remote_factory = mock.Mock(side_effect=AssertionError("must not connect"))

        report = sync_workspace_files(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            git_changed=True,
            remote_factory=remote_factory,
        )

        self.assertEqual(report.actions, ())
        remote_factory.assert_not_called()

    def test_git_is_never_called_without_filter_and_missing_git_fails_safely(
        self,
    ) -> None:
        self.write_config('{"name":"optional-git","host":"host","ignore":[".vscode"]}')
        fake = FakeRemote(snapshot({}))
        missing_git = FileNotFoundError("git")

        with mock.patch.object(
            file_sync_module.subprocess, "run", side_effect=missing_git
        ) as run_mock:
            report = sync_workspace_files(
                self.root,
                SyncDirection.LOCAL_TO_REMOTE,
                remote_factory=lambda _config: fake,
            )
        self.assertEqual(report.actions, ())
        run_mock.assert_not_called()

        remote_factory = mock.Mock(side_effect=AssertionError("must not connect"))
        with (
            mock.patch.object(
                file_sync_module.subprocess, "run", side_effect=missing_git
            ),
            self.assertRaisesRegex(FileSyncConfigError, "Git is not installed"),
        ):
            sync_workspace_files(
                self.root,
                SyncDirection.LOCAL_TO_REMOTE,
                git_changed=True,
                remote_factory=remote_factory,
            )
        remote_factory.assert_not_called()

    def test_git_filter_rejects_non_repository_and_non_upload_directions(self) -> None:
        self.write_config('{"name":"plain","host":"host"}')
        not_a_repository = subprocess.CompletedProcess(
            ["git"], 128, b"", b"fatal: not a git repository"
        )
        remote_factory = mock.Mock(side_effect=AssertionError("must not connect"))
        with (
            mock.patch.object(
                file_sync_module.subprocess,
                "run",
                return_value=not_a_repository,
            ),
            self.assertRaisesRegex(FileSyncConfigError, "not a git repository"),
        ):
            sync_workspace_files(
                self.root,
                SyncDirection.LOCAL_TO_REMOTE,
                git_changed=True,
                remote_factory=remote_factory,
            )
        remote_factory.assert_not_called()

        with self.assertRaisesRegex(FileSyncConfigError, "only supported"):
            sync_workspace_files(
                self.root,
                SyncDirection.BOTH,
                git_changed=True,
                remote_factory=remote_factory,
            )

    def test_crlf_and_lf_are_equivalent_in_all_sync_directions(self) -> None:
        for direction in SyncDirection:
            with self.subTest(direction=direction):
                project, remote_store, remote_file = self.create_content_project(
                    direction.value,
                    b"first\r\nsecond\r\n",
                    b"first\nsecond\n",
                )
                local_file = project / "shared.txt"

                report = sync_workspace_files(
                    project,
                    direction,
                    remote_factory=lambda config, store=remote_store: LocalSFTPRemote(
                        config, store
                    ),
                )

                self.assertEqual(report.actions, ())
                self.assertEqual(local_file.read_bytes(), b"first\r\nsecond\r\n")
                self.assertEqual(remote_file.read_bytes(), b"first\nsecond\n")

    def test_real_text_binary_and_lone_cr_differences_are_not_ignored(self) -> None:
        cases = {
            "text": (b"first\r\nlocal\n", b"first\nremote\n"),
            "lone-cr": (b"first\rsecond", b"first\nsecond"),
            "binary": (b"\0first\r\n", b"\0first\n"),
            "late-nul": (
                b"x" * CONTENT_CHUNK_SIZE + b"\0first\r\n",
                b"x" * CONTENT_CHUNK_SIZE + b"\0first\n",
            ),
        }
        for name, (local_content, remote_content) in cases.items():
            with self.subTest(name=name):
                project, remote_store, _remote_file = self.create_content_project(
                    f"different-{name}", local_content, remote_content
                )

                report = sync_workspace_files(
                    project,
                    SyncDirection.LOCAL_TO_REMOTE,
                    dry_run=True,
                    remote_factory=lambda config, store=remote_store: LocalSFTPRemote(
                        config, store
                    ),
                )

                self.assertEqual(
                    [(action.kind, action.path) for action in report.actions],
                    [(ActionKind.COPY_TO_REMOTE, PurePosixPath("shared.txt"))],
                )

    def test_crlf_normalization_handles_chunk_boundaries(self) -> None:
        prefix = b"x" * (CONTENT_CHUNK_SIZE - 1)
        project, remote_store, _remote_file = self.create_content_project(
            "chunk-boundary", prefix + b"\r\nend", prefix + b"\nend"
        )

        report = sync_workspace_files(
            project,
            SyncDirection.LOCAL_TO_REMOTE,
            dry_run=True,
            remote_factory=lambda config: LocalSFTPRemote(config, remote_store),
        )

        self.assertEqual(report.actions, ())

    def test_content_comparison_failure_happens_before_apply(self) -> None:
        (self.root / "shared.txt").write_text("local", encoding="utf-8")
        self.write_config('{"name":"failure","host":"host","ignore":[".vscode"]}')

        class CompareFailureRemote(FakeRemote):
            def equivalent_file_content(self, relative, _local_root):
                raise FileSyncError(
                    f"cannot compare local and remote content for {relative}"
                )

        fake = CompareFailureRemote(
            snapshot({"shared.txt": file_entry(mtime=1, size=5)})
        )

        with self.assertRaisesRegex(FileSyncError, "shared.txt"):
            sync_workspace_files(
                self.root,
                SyncDirection.LOCAL_TO_REMOTE,
                remote_factory=lambda _config: fake,
            )

        self.assertEqual(fake.ensure_root_calls, 0)
        self.assertEqual(fake.applied, [])

    def test_empty_source_still_creates_a_missing_destination_root(self) -> None:
        plan = build_sync_plan(
            SyncDirection.LOCAL_TO_REMOTE,
            snapshot({}),
            snapshot({}, exists=False),
            SyncOptions(),
        )

        self.assertEqual(
            [(action.kind, action.path) for action in plan],
            [(ActionKind.CREATE_REMOTE_DIRECTORY, PurePosixPath("."))],
        )

    def test_pack_reuses_only_local_config_fields_and_never_opens_sftp(
        self,
    ) -> None:
        project = self.root / "offline-config"
        source = project / "src"
        (project / ".vscode").mkdir(parents=True)
        source.mkdir()
        (project / ".syncignore").write_text("ignored-by-file.txt\n")
        (project / ".vscode" / "sftp.json").write_text(
            json.dumps(
                {
                    "name": "offline",
                    "host": {"invalid": "but unused"},
                    "password": ["also", "unused"],
                    "context": "src",
                    "ignore": ["*.tmp", "!keep.tmp"],
                    "ignoreFile": ".syncignore",
                    "defaultProfile": "portable",
                    "profiles": {"portable": {"ignore": ["*.log"]}},
                }
            ),
            encoding="utf-8",
        )
        for name in (
            "keep.txt",
            "keep.tmp",
            "drop.tmp",
            "drop.log",
            "ignored-by-file.txt",
        ):
            (source / name).write_text(name, encoding="utf-8")
        output = source / "portable.zip"

        with mock.patch.object(
            file_sync_module.SFTPRemote,
            "__enter__",
            side_effect=AssertionError("offline pack must not open SFTP"),
        ):
            report = pack_sync_bundle(project, output_path=output)

        self.assertEqual(report.local_root, source)
        self.assertEqual(report.manifest.mode, BundleMode.FULL)
        self.assertEqual(
            {entry.path.as_posix() for entry in report.manifest.entries},
            {"keep.txt", "keep.tmp"},
        )
        self.assertIn("/portable.zip", report.manifest.ignore_patterns)
        raw_manifest = self.read_bundle_manifest(output)
        self.assertEqual(raw_manifest["format"], BUNDLE_FORMAT)
        self.assertEqual(raw_manifest["version"], BUNDLE_VERSION)
        self.assertFalse(
            {"host", "username", "password", "source", "local_root"} & set(raw_manifest)
        )
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    BUNDLE_MANIFEST_PATH,
                    "payload/keep.txt",
                    "payload/keep.tmp",
                },
            )

        (project / ".vscode" / "sftp.json").write_text("{broken")
        bypass = self.root / "bypass.zip"
        pack_sync_bundle(project, no_config=True, output_path=bypass)
        self.assertTrue(bypass.is_file())
        with self.assertRaisesRegex(FileSyncConfigError, "mutually exclusive"):
            load_local_sync_selection(project, no_config=True, profile="portable")

    def test_pack_full_bundle_applies_files_directories_metadata_and_warnings(
        self,
    ) -> None:
        source = self.root / "full-source"
        nested = source / "nested"
        empty = source / "empty"
        nested.mkdir(parents=True)
        empty.mkdir()
        script = nested / "run.sh"
        script.write_bytes(b"#!/bin/sh\r\necho ok\r\n")
        script.chmod(0o751)
        binary = source / "data.bin"
        binary.write_bytes(b"\x00\x01\xff")
        mtime_ns = 1_700_000_000_123_456_789
        os.utime(script, ns=(mtime_ns, mtime_ns))
        (source / "shortcut").symlink_to("nested/run.sh")
        bundle = self.root / "full.zip"
        messages: list[str] = []

        with mock.patch.object(
            file_sync_module.SFTPRemote,
            "__enter__",
            side_effect=AssertionError("offline workflow must not open SFTP"),
        ):
            report = pack_sync_bundle(
                source,
                no_config=True,
                output_path=bundle,
                progress=messages.append,
            )
            target = self.root / "new-target"
            applied = apply_sync_bundle(bundle, target, progress=messages.append)

        self.assertEqual(
            report.manifest.skipped_symlinks,
            (PurePosixPath("shortcut"),),
        )
        self.assertTrue(
            any("warning: skipped symlink: shortcut" in line for line in messages)
        )
        self.assertEqual(
            (target / "nested" / "run.sh").read_bytes(), script.read_bytes()
        )
        self.assertEqual((target / "data.bin").read_bytes(), binary.read_bytes())
        self.assertTrue((target / "empty").is_dir())
        self.assertFalse(os.path.lexists(target / "shortcut"))
        self.assertEqual(
            (target / "nested" / "run.sh").stat().st_mtime_ns,
            script.stat().st_mtime_ns,
        )
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE((target / "nested" / "run.sh").stat().st_mode),
                0o751,
            )
        self.assertFalse(applied.dry_run)
        self.assertEqual(verify_sync_bundle(bundle).mode, BundleMode.FULL)

    def test_pack_dry_run_force_and_atomic_failure_preserve_existing_output(
        self,
    ) -> None:
        source = self.root / "atomic-source"
        source.mkdir()
        (source / "file.txt").write_text("new", encoding="utf-8")
        bundle = self.root / "atomic.zip"
        bundle.write_bytes(b"original")

        with self.assertRaisesRegex(FileSyncConfigError, "already exists"):
            pack_sync_bundle(source, no_config=True, output_path=bundle)

        dry_messages: list[str] = []
        report = pack_sync_bundle(
            source,
            no_config=True,
            output_path=bundle,
            force=True,
            dry_run=True,
            progress=dry_messages.append,
        )
        self.assertTrue(report.dry_run)
        self.assertEqual(bundle.read_bytes(), b"original")
        self.assertIn(f"would overwrite bundle: {bundle}", dry_messages)

        with mock.patch(
            "hagency_cli.file_sync.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaisesRegex(FileSyncError, "cannot write sync bundle"):
                pack_sync_bundle(
                    source,
                    no_config=True,
                    output_path=bundle,
                    force=True,
                )
        self.assertEqual(bundle.read_bytes(), b"original")
        self.assertFalse(
            any(
                path.name.startswith(".atomic.zip.hgc-") for path in self.root.iterdir()
            )
        )

    def test_empty_full_bundle_dry_run_and_delete_preserve_target_root(self) -> None:
        source = self.root / "empty-bundle-source"
        source.mkdir()
        bundle = self.root / "empty-full.zip"
        report = pack_sync_bundle(source, no_config=True, output_path=bundle)
        self.assertEqual(report.manifest.entries, ())

        missing_target = self.root / "missing-empty-target"
        apply_sync_bundle(bundle, missing_target, delete=True, dry_run=True)
        self.assertFalse(missing_target.exists())

        missing_target.mkdir()
        (missing_target / "extra.txt").write_text("extra", encoding="utf-8")
        apply_sync_bundle(bundle, missing_target, delete=True)
        self.assertTrue(missing_target.is_dir())
        self.assertEqual(list(missing_target.iterdir()), [])

    def test_apply_full_bundle_honors_options_delete_and_line_endings(self) -> None:
        source = self.root / "option-source"
        source.mkdir()
        (source / "text.txt").write_bytes(b"one\r\ntwo\r\n")
        binary = source / "binary.bin"
        binary.write_bytes(b"one\0\r\n")
        binary.chmod(0o755)
        (source / "new.txt").write_text("new", encoding="utf-8")
        (source / "ignored.tmp").write_text("source", encoding="utf-8")
        bundle = self.root / "options.zip"
        pack_sync_bundle(
            source,
            no_config=True,
            output_path=bundle,
            exclude=("*.tmp",),
        )

        target = self.root / "option-target"
        (target / ".vscode").mkdir(parents=True)
        (target / "text.txt").write_bytes(b"one\ntwo\n")
        (target / "binary.bin").write_bytes(b"one\0\n")
        (target / "binary.bin").chmod(0o600)
        (target / "extra.txt").write_text("extra", encoding="utf-8")
        (target / "ignored.tmp").write_text("target", encoding="utf-8")
        (target / ".vscode" / "sftp.json").write_text("protected")

        before = {
            path.relative_to(target).as_posix(): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        }
        dry_report = apply_sync_bundle(bundle, target, delete=True, dry_run=True)
        after_dry = {
            path.relative_to(target).as_posix(): path.read_bytes()
            for path in target.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after_dry, before)
        self.assertNotIn(
            PurePosixPath("text.txt"),
            {action.path for action in dry_report.actions},
        )
        self.assertIn(
            PurePosixPath("binary.bin"),
            {action.path for action in dry_report.actions},
        )

        apply_sync_bundle(bundle, target, delete=True)
        self.assertEqual((target / "text.txt").read_bytes(), b"one\ntwo\n")
        self.assertEqual((target / "binary.bin").read_bytes(), b"one\0\r\n")
        self.assertFalse((target / "extra.txt").exists())
        self.assertEqual((target / "ignored.tmp").read_text(), "target")
        self.assertEqual((target / ".vscode" / "sftp.json").read_text(), "protected")
        if os.name != "nt":
            self.assertEqual(
                stat.S_IMODE((target / "binary.bin").stat().st_mode), 0o600
            )

        option_target = self.root / "skip-target"
        option_target.mkdir()
        existing = option_target / "text.txt"
        existing.write_text("destination", encoding="utf-8")
        apply_sync_bundle(
            bundle,
            option_target,
            skip_create=True,
            ignore_existing=True,
        )
        self.assertEqual(existing.read_text(), "destination")
        self.assertFalse((option_target / "new.txt").exists())

        newer_target = self.root / "newer-target"
        newer_target.mkdir()
        newer = newer_target / "text.txt"
        newer.write_text("newer destination", encoding="utf-8")
        future = (source / "text.txt").stat().st_mtime + 100
        os.utime(newer, (future, future))
        apply_sync_bundle(bundle, newer_target, update=True)
        self.assertEqual(newer.read_text(), "newer destination")
        self.assertEqual((newer_target / "new.txt").read_text(), "new")

    def test_git_patch_bundle_limits_contents_and_authorized_deletions(self) -> None:
        source = self.root / "git-source"
        source.mkdir()
        self.initialize_git_repository_at(source)
        (source / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        for name in ("changed.txt", "deleted.txt", "old-name.txt", "stable.txt"):
            (source / name).write_text(f"base {name}", encoding="utf-8")
        self.run_git("add", ".", cwd=source)
        self.run_git("commit", "-m", "base", "--quiet", cwd=source)

        (source / "changed.txt").write_text("changed", encoding="utf-8")
        (source / "deleted.txt").unlink()
        (source / "old-name.txt").rename(source / "new-name.txt")
        (source / "untracked.txt").write_text("untracked", encoding="utf-8")
        (source / "ignored.txt").write_text("ignored", encoding="utf-8")
        (source / "link").symlink_to("stable.txt")
        bundle = self.root / "git-patch.zip"
        report = pack_sync_bundle(
            source,
            no_config=True,
            output_path=bundle,
            git_changed=True,
        )

        self.assertEqual(report.manifest.mode, BundleMode.GIT_PATCH)
        self.assertEqual(
            {entry.path.as_posix() for entry in report.manifest.entries},
            {"changed.txt", "new-name.txt", "untracked.txt"},
        )
        self.assertEqual(
            {path.as_posix() for path in report.manifest.deletions},
            {"deleted.txt", "old-name.txt"},
        )
        self.assertEqual(
            report.manifest.skipped_symlinks,
            (PurePosixPath("link"),),
        )

        def make_target(name: str) -> Path:
            target = self.root / name
            target.mkdir()
            for filename in ("deleted.txt", "old-name.txt", "unrelated.txt"):
                (target / filename).write_text(f"target {filename}", encoding="utf-8")
            return target

        retained = make_target("patch-retained")
        apply_sync_bundle(bundle, retained)
        self.assertTrue((retained / "deleted.txt").exists())
        self.assertTrue((retained / "old-name.txt").exists())
        self.assertTrue((retained / "unrelated.txt").exists())

        deleted = make_target("patch-deleted")
        apply_sync_bundle(bundle, deleted, delete=True)
        self.assertFalse((deleted / "deleted.txt").exists())
        self.assertFalse((deleted / "old-name.txt").exists())
        self.assertTrue((deleted / "unrelated.txt").exists())
        self.assertEqual((deleted / "new-name.txt").read_text(), "base old-name.txt")

        clean = self.root / "clean-repo"
        clean.mkdir()
        self.initialize_git_repository_at(clean)
        (clean / "tracked.txt").write_text("tracked", encoding="utf-8")
        self.run_git("add", ".", cwd=clean)
        self.run_git("commit", "-m", "clean", "--quiet", cwd=clean)
        clean_output = self.root / "must-not-exist.zip"
        clean_report = pack_sync_bundle(
            clean,
            no_config=True,
            output_path=clean_output,
            git_changed=True,
        )
        self.assertIsNone(clean_report.output_path)
        self.assertFalse(clean_output.exists())

        parent_delete_bundle = self.root / "parent-delete.zip"
        parent_delete_manifest = self.read_bundle_manifest(bundle)
        parent_delete_manifest["entries"] = []
        parent_delete_manifest["deletions"] = ["parent/child.txt"]
        parent_delete_manifest["skipped_symlinks"] = []
        with zipfile.ZipFile(parent_delete_bundle, "w") as archive:
            archive.writestr(
                BUNDLE_MANIFEST_PATH,
                json.dumps(parent_delete_manifest).encode(),
            )
        parent_target = self.root / "parent-delete-target"
        parent_target.mkdir()
        (parent_target / "parent").write_text("must stay", encoding="utf-8")
        apply_sync_bundle(parent_delete_bundle, parent_target, delete=True)
        self.assertEqual((parent_target / "parent").read_text(), "must stay")

    def test_apply_rejects_invalid_bundle_before_any_target_write(self) -> None:
        source = self.root / "validation-source"
        source.mkdir()
        (source / "first.txt").write_text("first", encoding="utf-8")
        (source / "second.txt").write_text("second", encoding="utf-8")
        original = self.root / "validation.zip"
        pack_sync_bundle(source, no_config=True, output_path=original)

        invalid_bundles: list[tuple[str, Path]] = []

        checksum = self.root / "bad-checksum.zip"
        shutil.copyfile(original, checksum)
        self.rewrite_bundle(checksum, replacements={"payload/second.txt": b"xxxxxx"})
        invalid_bundles.append(("checksum mismatch", checksum))

        extra = self.root / "extra-entry.zip"
        shutil.copyfile(original, extra)
        self.rewrite_bundle(extra, extras={"payload/unlisted.txt": b"extra"})
        invalid_bundles.append(("does not match manifest", extra))

        unsafe = self.root / "unsafe-path.zip"
        shutil.copyfile(original, unsafe)
        unsafe_manifest = self.read_bundle_manifest(unsafe)
        unsafe_manifest["entries"][0]["path"] = "../escape.txt"
        self.rewrite_bundle(unsafe, manifest=unsafe_manifest)
        invalid_bundles.append(("safe relative path", unsafe))

        windows_path = self.root / "windows-path.zip"
        shutil.copyfile(original, windows_path)
        windows_manifest = self.read_bundle_manifest(windows_path)
        windows_manifest["entries"][0]["path"] = "C:/escape.txt"
        self.rewrite_bundle(windows_path, manifest=windows_manifest)
        invalid_bundles.append(("safe relative path", windows_path))

        missing_parent = self.root / "missing-parent.zip"
        shutil.copyfile(original, missing_parent)
        missing_parent_manifest = self.read_bundle_manifest(missing_parent)
        missing_parent_manifest["entries"][0]["path"] = "nested/first.txt"
        self.rewrite_bundle(missing_parent, manifest=missing_parent_manifest)
        invalid_bundles.append(("missing parent directory", missing_parent))

        collision = self.root / "case-collision.zip"
        shutil.copyfile(original, collision)
        collision_manifest = self.read_bundle_manifest(collision)
        collision_manifest["entries"][1]["path"] = "FIRST.TXT"
        self.rewrite_bundle(collision, manifest=collision_manifest)
        invalid_bundles.append(("collide across platforms", collision))

        unicode_collision = self.root / "unicode-collision.zip"
        shutil.copyfile(original, unicode_collision)
        unicode_manifest = self.read_bundle_manifest(unicode_collision)
        unicode_manifest["entries"][0]["path"] = "café.txt"
        unicode_manifest["entries"][1]["path"] = "café.txt"
        self.rewrite_bundle(unicode_collision, manifest=unicode_manifest)
        invalid_bundles.append(("collide across platforms", unicode_collision))

        unknown = self.root / "unknown-version.zip"
        shutil.copyfile(original, unknown)
        unknown_manifest = self.read_bundle_manifest(unknown)
        unknown_manifest["version"] = BUNDLE_VERSION + 1
        self.rewrite_bundle(unknown, manifest=unknown_manifest)
        invalid_bundles.append(("unsupported sync bundle version", unknown))

        duplicate = self.root / "duplicate-entry.zip"
        shutil.copyfile(original, duplicate)
        with mock.patch("warnings.warn"), zipfile.ZipFile(duplicate, "a") as archive:
            archive.writestr("payload/first.txt", b"first")
        invalid_bundles.append(("duplicate ZIP entries", duplicate))

        target = self.root / "validation-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("untouched", encoding="utf-8")
        for expected, bundle in invalid_bundles:
            with self.subTest(bundle=bundle.name):
                with self.assertRaisesRegex(FileSyncConfigError, expected):
                    apply_sync_bundle(bundle, target, delete=True)
                self.assertEqual(sentinel.read_text(), "untouched")
                self.assertFalse((target / "first.txt").exists())
                self.assertFalse((self.root / "escape.txt").exists())

    def test_apply_refuses_to_replace_the_bundle_through_an_ancestor(self) -> None:
        source = self.root / "bundle-ancestor-source"
        source.mkdir()
        (source / "packages").write_text("source file", encoding="utf-8")
        outside_bundle = self.root / "ancestor.zip"
        pack_sync_bundle(source, no_config=True, output_path=outside_bundle)

        target = self.root / "bundle-ancestor-target"
        bundle = target / "packages" / "bundle.zip"
        bundle.parent.mkdir(parents=True)
        shutil.copyfile(outside_bundle, bundle)

        with self.assertRaisesRegex(FileSyncConfigError, "managed path"):
            apply_sync_bundle(bundle, target)
        self.assertTrue(bundle.is_file())

    def test_apply_does_not_replace_a_directory_with_ignored_contents(self) -> None:
        source = self.root / "protected-conflict-source"
        source.mkdir()
        (source / "folder").write_text("source file", encoding="utf-8")
        bundle = self.root / "protected-conflict.zip"
        pack_sync_bundle(
            source,
            no_config=True,
            output_path=bundle,
            exclude=("/folder/keep.txt",),
        )

        target = self.root / "protected-conflict-target"
        protected = target / "folder" / "keep.txt"
        protected.parent.mkdir(parents=True)
        protected.write_text("protected", encoding="utf-8")

        with self.assertRaisesRegex(FileSyncError, "containing an ignored path"):
            apply_sync_bundle(bundle, target)
        self.assertEqual(protected.read_text(), "protected")
        self.assertTrue((target / "folder").is_dir())

    def test_cli_pack_output_and_apply_target_default_to_invocation_directory(
        self,
    ) -> None:
        source = self.root / "default-source"
        source.mkdir()
        (source / "file.txt").write_text("portable", encoding="utf-8")
        invocation = self.root / "invocation"
        invocation.mkdir()
        old_cwd = Path.cwd()
        try:
            os.chdir(invocation)
            pack_result = CliRunner().invoke(
                cli.app,
                ["sync", "pack", "--root", str(source), "--no-config"],
                prog_name="hgc",
                catch_exceptions=False,
            )
            apply_result = CliRunner().invoke(
                cli.app,
                ["sync", "apply", "hgc-sync.zip"],
                prog_name="hgc",
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(pack_result.exit_code, 0, pack_result.stderr)
        self.assertIn(
            f"created bundle: {invocation / 'hgc-sync.zip'}", pack_result.stdout
        )
        self.assertEqual(apply_result.exit_code, 0, apply_result.stderr)
        self.assertEqual((invocation / "file.txt").read_text(), "portable")
        self.assertTrue((invocation / "hgc-sync.zip").is_file())

    def test_cli_dispatches_pack_and_apply_without_sync_transport(self) -> None:
        bundle = self.root / "bundle.zip"
        identity = self.root / "not-used"
        with mock.patch.object(cli, "pack_sync_bundle") as pack_mock:
            pack_result = CliRunner().invoke(
                cli.app,
                [
                    "sync",
                    "pack",
                    "--root",
                    str(self.root),
                    "--no-config",
                    "--output",
                    str(bundle),
                    "--force",
                    "--git-changed",
                    "--exclude",
                    "*.tmp",
                    "--exclude",
                    "build/",
                    "--dry-run",
                ],
                prog_name="hgc",
                catch_exceptions=False,
            )

        self.assertEqual(pack_result.exit_code, 0, pack_result.stderr)
        pack_mock.assert_called_once_with(
            self.root,
            profile=None,
            no_config=True,
            output_path=bundle,
            force=True,
            git_changed=True,
            exclude=["*.tmp", "build/"],
            dry_run=True,
            progress=print,
        )

        with mock.patch.object(cli, "apply_sync_bundle") as apply_mock:
            apply_result = CliRunner().invoke(
                cli.app,
                [
                    "sync",
                    "apply",
                    str(bundle),
                    "--root",
                    str(self.root),
                    "--delete",
                    "--skip-create",
                    "--ignore-existing",
                    "--update",
                    "--dry-run",
                ],
                prog_name="hgc",
                catch_exceptions=False,
            )

        self.assertEqual(apply_result.exit_code, 0, apply_result.stderr)
        apply_mock.assert_called_once_with(
            bundle,
            self.root,
            delete=True,
            skip_create=True,
            ignore_existing=True,
            update=True,
            dry_run=True,
            progress=print,
        )

        rejected = CliRunner().invoke(
            cli.app,
            ["sync", "pack", "server:/srv", "--identity", str(identity)],
            prog_name="hgc",
            catch_exceptions=False,
        )
        self.assertEqual(rejected.exit_code, 2)

    def test_cli_dispatches_sync_from_invocation_directory(self) -> None:
        config = mock.Mock()
        report = SyncReport(
            config=config,
            direction=SyncDirection.BOTH,
            actions=(),
            dry_run=True,
        )
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            invocation_root = Path.cwd()
            with mock.patch.object(
                cli, "sync_workspace_files", return_value=report
            ) as sync_mock:
                result = CliRunner().invoke(
                    cli.app,
                    ["sync", "both", "--dry-run"],
                    prog_name="hgc",
                    catch_exceptions=False,
                )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(result.exit_code, 0, result.stderr)
        sync_mock.assert_called_once_with(
            invocation_root,
            SyncDirection.BOTH,
            profile=None,
            remote_endpoint=None,
            port=None,
            identity=None,
            exclude=(),
            delete=False,
            skip_create=False,
            ignore_existing=False,
            update=False,
            git_changed=False,
            dry_run=True,
            progress=print,
        )
        self.assertIn("already in sync", result.stdout)

    def test_cli_dispatches_git_changed_only_for_local_to_remote(self) -> None:
        report = SyncReport(
            config=mock.Mock(),
            direction=SyncDirection.LOCAL_TO_REMOTE,
            actions=(),
            dry_run=True,
        )
        with mock.patch.object(
            cli, "sync_workspace_files", return_value=report
        ) as sync_mock:
            result = CliRunner().invoke(
                cli.app,
                [
                    "sync",
                    "local-to-remote",
                    "--root",
                    str(self.root),
                    "--git-changed",
                    "--dry-run",
                ],
                prog_name="hgc",
                catch_exceptions=False,
            )

        self.assertEqual(result.exit_code, 0, result.stderr)
        sync_mock.assert_called_once_with(
            self.root,
            SyncDirection.LOCAL_TO_REMOTE,
            profile=None,
            remote_endpoint=None,
            port=None,
            identity=None,
            exclude=(),
            delete=False,
            skip_create=False,
            ignore_existing=False,
            update=False,
            git_changed=True,
            dry_run=True,
            progress=print,
        )

        rejected = CliRunner().invoke(
            cli.app,
            ["sync", "both", "--git-changed"],
            prog_name="hgc",
            catch_exceptions=False,
        )
        self.assertEqual(rejected.exit_code, 2)
        self.assertIn("No such option", rejected.stderr)

    def test_cli_short_sync_commands_dispatch_with_matching_options(self) -> None:
        cases = (
            ("l2r", SyncDirection.LOCAL_TO_REMOTE, ["--git-changed"], True),
            ("r2l", SyncDirection.REMOTE_TO_LOCAL, [], False),
        )
        for command, direction, options, git_changed in cases:
            with self.subTest(command=command):
                report = SyncReport(
                    config=mock.Mock(),
                    direction=direction,
                    actions=(),
                    dry_run=True,
                )
                with mock.patch.object(
                    cli, "sync_workspace_files", return_value=report
                ) as sync_mock:
                    result = CliRunner().invoke(
                        cli.app,
                        [
                            "sync",
                            command,
                            "--root",
                            str(self.root),
                            *options,
                            "--dry-run",
                        ],
                        prog_name="hgc",
                        catch_exceptions=False,
                    )

                self.assertEqual(result.exit_code, 0, result.stderr)
                sync_mock.assert_called_once_with(
                    self.root,
                    direction,
                    profile=None,
                    remote_endpoint=None,
                    port=None,
                    identity=None,
                    exclude=(),
                    delete=False,
                    skip_create=False,
                    ignore_existing=False,
                    update=False,
                    git_changed=git_changed,
                    dry_run=True,
                    progress=print,
                )

    def test_cli_dispatches_temporary_endpoints_and_direction_options(self) -> None:
        identity = self.root / "id_ed25519"
        cases = (
            (
                "l2r",
                SyncDirection.LOCAL_TO_REMOTE,
                "dev@server:/srv/upload",
                [
                    "-P",
                    "2222",
                    "-i",
                    str(identity),
                    "--exclude",
                    "*.tmp",
                    "--exclude",
                    "build/",
                    "--delete",
                    "--skip-create",
                    "--ignore-existing",
                    "--update",
                    "--git-changed",
                ],
                {
                    "port": 2222,
                    "identity": identity,
                    "exclude": ["*.tmp", "build/"],
                    "delete": True,
                    "skip_create": True,
                    "ignore_existing": True,
                    "update": True,
                    "git_changed": True,
                },
            ),
            (
                "remote-to-local",
                SyncDirection.REMOTE_TO_LOCAL,
                "server:~/restore",
                ["--delete", "--update"],
                {
                    "port": None,
                    "identity": None,
                    "exclude": (),
                    "delete": True,
                    "skip_create": False,
                    "ignore_existing": False,
                    "update": True,
                    "git_changed": False,
                },
            ),
            (
                "both",
                SyncDirection.BOTH,
                "[2001:db8::1]:C:/Projects/ws",
                ["--skip-create", "--ignore-existing"],
                {
                    "port": None,
                    "identity": None,
                    "exclude": (),
                    "delete": False,
                    "skip_create": True,
                    "ignore_existing": True,
                    "update": False,
                    "git_changed": False,
                },
            ),
        )

        for command, direction, endpoint, options, expected in cases:
            with self.subTest(command=command):
                report = SyncReport(
                    config=mock.Mock(),
                    direction=direction,
                    actions=(),
                    dry_run=True,
                )
                with mock.patch.object(
                    cli, "sync_workspace_files", return_value=report
                ) as sync_mock:
                    result = CliRunner().invoke(
                        cli.app,
                        [
                            "sync",
                            command,
                            endpoint,
                            "--root",
                            str(self.root),
                            *options,
                            "--dry-run",
                        ],
                        prog_name="hgc",
                        catch_exceptions=False,
                    )

                self.assertEqual(result.exit_code, 0, result.stderr)
                sync_mock.assert_called_once_with(
                    self.root,
                    direction,
                    profile=None,
                    remote_endpoint=endpoint,
                    port=expected["port"],
                    identity=expected["identity"],
                    exclude=expected["exclude"],
                    delete=expected["delete"],
                    skip_create=expected["skip_create"],
                    ignore_existing=expected["ignore_existing"],
                    update=expected["update"],
                    git_changed=expected["git_changed"],
                    dry_run=True,
                    progress=print,
                )

    def test_cli_rejects_temporary_options_without_endpoint(self) -> None:
        for command in ("local-to-remote", "l2r", "remote-to-local", "r2l", "both"):
            options = [
                ["--port", "2222"],
                ["--identity", "id_ed25519"],
                ["--exclude", "*.log"],
                ["--skip-create"],
                ["--ignore-existing"],
            ]
            if command != "both":
                options.extend((["--delete"], ["--update"]))
            for arguments in options:
                with (
                    self.subTest(command=command, arguments=arguments),
                    mock.patch.object(file_sync_module, "load_sftp_config") as loader,
                    mock.patch.object(SFTPRemote, "__enter__") as connect,
                ):
                    result = CliRunner().invoke(
                        cli.app,
                        ["sync", command, "--root", str(self.root), *arguments],
                        prog_name="hgc",
                        catch_exceptions=False,
                    )
                self.assertEqual(result.exit_code, 2, result.stderr)
                self.assertIn("temporary", result.stderr)
                self.assertIn("REMOTE", result.stderr)
                self.assertIn(".vscode/sftp.json", result.stderr)
                loader.assert_not_called()
                connect.assert_not_called()

    def test_cli_rejects_profile_with_temporary_endpoint(self) -> None:
        result = CliRunner().invoke(
            cli.app,
            ["sync", "r2l", "server:/srv", "--profile", "prod"],
            prog_name="hgc",
            catch_exceptions=False,
        )

        self.assertEqual(result.exit_code, 2)
        self.assertIn("mutually exclusive", result.stderr)

    def test_cli_initializes_current_or_selected_project_without_connecting(
        self,
    ) -> None:
        selected = self.root / "selected"
        selected.mkdir()
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            with mock.patch.object(cli, "sync_workspace_files") as sync_mock:
                current_result = CliRunner().invoke(
                    cli.app,
                    ["sync", "init"],
                    prog_name="hgc",
                    catch_exceptions=False,
                )
                selected_result = CliRunner().invoke(
                    cli.app,
                    [
                        "sync",
                        "init",
                        "--root",
                        str(selected),
                        "--dry-run",
                    ],
                    prog_name="hgc",
                    catch_exceptions=False,
                )
        finally:
            os.chdir(old_cwd)

        self.assertEqual(current_result.exit_code, 0, current_result.stderr)
        self.assertTrue((self.root / ".vscode" / "sftp.json").is_file())
        self.assertIn("initialized SFTP config:", current_result.stdout)
        self.assertEqual(selected_result.exit_code, 0, selected_result.stderr)
        self.assertIn("Would create SFTP config:", selected_result.stdout)
        self.assertFalse((selected / ".vscode").exists())
        sync_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
