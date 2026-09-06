from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from typer.main import get_command
from typer.testing import CliRunner

from hagency_cli import cli
from hagency_cli.commands import completion
from hagency_cli.files.purge import removal, roots
from hagency_cli.files.purge.models import PurgeRequest
from hagency_cli.files.purge.operations import purge_space
from hagency_cli.files.sync.config import build_temporary_sftp_config
from hagency_cli.files.sync.models import FileSyncError, SyncDirection
from hagency_cli.files.sync.operations import sync_workspace_files
from hagency_cli.files.sync.sftp import SFTPRemote
from hagency_cli.workspace import git
from hagency_cli.workspace.catalog import list_profile_selected_links
from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import SourceBatchError, WorkspaceError
from hagency_cli.workspace.operations.sources import sync_sources_with_progress
from hagency_cli.workspace.operations.skills import add_skills
from hagency_cli.workspace.sources import Remote, Source, validate_git_url

if __package__:
    from .support import LocalSFTPRemote
else:
    from support import LocalSFTPRemote


class ReviewFollowupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def connection_config(self, **changes):
        return replace(
            build_temporary_sftp_config(self.root, "host:/srv"),
            ssh_config_path=self.root / "missing-ssh-config",
            **changes,
        )

    @unittest.skipIf(os.name == "nt", "Windows cannot remove the working directory")
    def test_purge_reports_a_vanished_working_directory_without_traceback(self):
        cwd = self.root / "vanished"
        cwd.mkdir()
        with contextlib.chdir(cwd):
            cwd.rmdir()
            result = CliRunner().invoke(cli.app, ["file", "purge", "."])
        self.assertEqual(result.exit_code, 1)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn("Error:", result.stderr)

    def test_purge_reports_inaccessible_home_during_root_resolution(self):
        with mock.patch(
            "hagency_cli.files.purge.roots.Path.resolve",
            side_effect=PermissionError("home is inaccessible"),
        ):
            result = CliRunner().invoke(cli.app, ["file", "purge", "--paths"])
        self.assertEqual(result.exit_code, 1)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertIn("home is inaccessible", result.stderr)

    def test_explicit_ssh_agents_do_not_modify_process_environment(self):
        rendezvous = threading.Barrier(2)
        observed = []
        clients = []
        agents = {"agent-a": mock.Mock(), "agent-b": mock.Mock()}

        def make_client():
            client = mock.Mock()

            def connect(**_kwargs):
                rendezvous.wait(timeout=5)
                observed.append(os.environ.get("SSH_AUTH_SOCK"))
                rendezvous.wait(timeout=5)

            client.connect.side_effect = connect
            clients.append(client)
            return client

        def connect(agent):
            with SFTPRemote(self.connection_config(agent=agent)):
                pass

        with (
            mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "original"}),
            mock.patch("paramiko.SSHClient", side_effect=make_client),
            mock.patch(
                "hagency_cli.files.sync.sftp._connect_agent",
                side_effect=lambda path, timeout: agents[path],
                create=True,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            list(executor.map(connect, agents))
            self.assertEqual(os.environ["SSH_AUTH_SOCK"], "original")
        self.assertEqual(observed, ["original", "original"])
        self.assertCountEqual([client._agent for client in clients], agents.values())
        for client in clients:
            client.close.assert_called_once()

    def test_failed_sftp_connect_closes_the_client(self):
        client = mock.Mock()
        client.connect.side_effect = OSError("connection refused")
        with mock.patch("paramiko.SSHClient", return_value=client):
            with self.assertRaisesRegex(FileSyncError, "connection refused"):
                with SFTPRemote(self.connection_config()):
                    self.fail("connection should fail")
        client.close.assert_called_once()

    def test_git_timeout_becomes_a_business_error(self):
        with self.assertRaisesRegex(WorkspaceError, "timed out"):
            git.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.05)

    def test_source_timeout_is_reported_and_next_source_is_processed(self):
        sources = [
            Source("slow", self.root / "slow", Remote("origin", "unused", "main")),
            Source("local", self.root, None),
        ]
        events = []
        with (
            mock.patch(
                "hagency_cli.workspace.git.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git", "clone"], 300),
            ),
            self.assertRaises(SourceBatchError) as error,
        ):
            sync_sources_with_progress(
                list(enumerate(sources, 1)),
                total=2,
                dry_run=False,
                depth=None,
                progress=events.append,
            )
        self.assertEqual(error.exception.failed, ("slow",))
        self.assertTrue(any("timed out" in event.message for event in events))
        self.assertTrue(any("[2/2] local" in event.message for event in events))

    def test_skill_acquisition_timeout_retains_registration_and_skips_install(self):
        config = self.root / "hagency-config.toml"
        write_toml(config, {"defaults": {"checkout_dir": "checkouts", "depth": 1}})
        destination = self.root / "installed"
        with (
            mock.patch(
                "hagency_cli.workspace.git.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git", "clone"], 300),
            ),
            self.assertRaisesRegex(WorkspaceError, "timed out.*registration.*retained"),
        ):
            add_skills(
                skill="owner/repository",
                cwd=self.root,
                root_value=str(self.root),
                skills_path=str(destination),
                all_skills=True,
            )
        self.assertIn("repository", read_toml(config)["source"])
        self.assertFalse(destination.exists())

    def test_service_config_completion_offers_files_and_directories(self):
        (self.root / "proxy.toml").write_text("")
        (self.root / "profiles").mkdir()
        command = get_command(cli.app).commands["service"].commands["model-proxy"]
        for action in ("start", "stop", "restart"):
            with self.subTest(action=action), contextlib.chdir(self.root):
                parameter = next(
                    p for p in command.commands[action].params if p.name == "config"
                )
                items = parameter.shell_complete(None, "pr")
                self.assertCountEqual(
                    [item.value for item in items], ["proxy.toml", "profiles" + os.sep]
                )

    def test_completion_option_at_end_does_not_index_past_words(self):
        for words in ("", "--root", "hgc skill add --root", "hgc --root '"):
            with (
                self.subTest(words=words),
                mock.patch.dict(os.environ, {"COMP_WORDS": words}),
            ):
                completion._raw_option_value("--root")
        with mock.patch.dict(os.environ, {"COMP_WORDS": "hgc --root './a b'"}):
            self.assertEqual(completion._raw_option_value("--root"), "./a b")

    def test_urlsplit_defers_port_validation_until_property_access(self):
        for port in ("bad", "99999"):
            url = f"https://github.com:{port}/owner/repo"
            parsed = urlsplit(url)
            with self.assertRaises(ValueError):
                _ = parsed.port
            with self.assertRaises(WorkspaceError):
                validate_git_url(url)

    def test_source_failure_is_printed_before_reanchor_tip(self):
        with mock.patch(
            "hagency_cli.commands.source.sync_selected_sources",
            side_effect=SourceBatchError(("demo",), ("demo",)),
        ):
            result = CliRunner().invoke(cli.app, ["source", "sync"])
        self.assertEqual(result.exit_code, 1)
        self.assertLess(result.stderr.index("Error:"), result.stderr.index("Tip:"))

    def test_profile_remove_completion_uses_the_profile_catalog(self):
        write_toml(self.root / "hagency-config.toml", {})
        profile = self.root / "profiles/demo"
        profile.mkdir(parents=True)
        write_toml(profile / "config.toml", {"name": "demo"})
        tree = get_command(cli.app)
        for group, action in (("profile", "remove"), ("p", "rm")):
            command = tree.commands[group].commands[action]
            context = command.make_context(
                action, ["--root", str(self.root)], resilient_parsing=True
            )
            parameter = next(p for p in command.params if p.name == "name")
            with mock.patch.dict(os.environ, {"COMP_WORDS": f"hgc {group} {action}"}):
                self.assertIn(
                    "demo",
                    [item.value for item in parameter.shell_complete(context, "d")],
                )

    def test_toml_null_is_rejected_before_existing_config_changes(self):
        config = self.root / "config.toml"
        config.write_text("name = 'original'\n")
        with self.assertRaisesRegex(
            WorkspaceError, "unsupported TOML value type: NoneType"
        ):
            write_toml(config, {"skill": {"demo": {"include": [None]}}})
        self.assertEqual(config.read_text(), "name = 'original'\n")
        self.assertFalse((self.root / ".config.toml.tmp").exists())

    def test_empty_profile_include_retains_existing_all_skills_semantics(self):
        skill = self.root / "demo"
        skill.mkdir()
        (skill / "SKILL.md").write_text("fixture")
        source = Source("fixture", self.root, None)
        self.assertEqual(
            list_profile_selected_links({"include": []}, source),
            list_profile_selected_links({"include": ["*"]}, source),
        )
        self.assertEqual(len(list_profile_selected_links({"include": []}, source)), 1)

    def test_vanishing_root_component_becomes_a_reported_issue(self):
        with mock.patch.object(
            roots,
            "_has_link_or_reparse_component",
            side_effect=FileNotFoundError("vanished"),
        ):
            report = purge_space(
                PurgeRequest(paths=(self.root,)),
                ui=mock.Mock(is_interactive=lambda: False),
            )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.issues[0].code, "invalid_root")
        self.assertIn("vanished", report.issues[0].message)

    @unittest.skipIf(os.name == "nt", "requires local symlink privileges")
    def test_sftp_preserves_external_symlinks_without_accessing_their_targets(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "secret"
        sentinel.write_bytes(b"must remain unchanged")
        for direction in (SyncDirection.LOCAL_TO_REMOTE, SyncDirection.REMOTE_TO_LOCAL):
            with self.subTest(direction=direction):
                project = self.root / (direction.value + "-local")
                store = self.root / (direction.value + "-remote")
                project.mkdir()
                store.mkdir()
                source, destination = (
                    (project, store)
                    if direction is SyncDirection.LOCAL_TO_REMOTE
                    else (store, project)
                )
                (source / "external").symlink_to(outside, target_is_directory=True)
                (source / "dangling").symlink_to("../nonexistent")
                report = sync_workspace_files(
                    project,
                    direction,
                    remote_endpoint="host:/",
                    remote_factory=lambda config: LocalSFTPRemote(config, store),
                )
                self.assertEqual(os.readlink(destination / "external"), str(outside))
                self.assertEqual(
                    os.readlink(destination / "dangling"), "../nonexistent"
                )
                self.assertFalse(
                    any("secret" in action.path.parts for action in report.actions)
                )
                self.assertEqual(sentinel.read_bytes(), b"must remain unchanged")

    @unittest.skipUnless(
        os.rmdir in os.supports_dir_fd, "requires POSIX fd-based removal"
    )
    def test_final_purge_rmdir_preserves_recreated_original_paths(
        self,
    ):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_text("protected")
        real_rmdir = os.rmdir
        for replacement in ("symlink", "nonempty", "empty"):
            project = self.root / replacement
            project.mkdir()
            (project / "package.json").write_text("{}")
            artifact = project / "node_modules"
            artifact.mkdir()
            (artifact / "old").write_text("rebuildable")
            swapped = []

            def replace_at_final_rmdir(name, *, dir_fd=None):
                if dir_fd is not None and (
                    name == "node_modules" or str(name).startswith(".hagency-purge-")
                ):
                    if artifact.exists():
                        artifact.rename(project / "original-empty")
                    if replacement == "symlink":
                        artifact.symlink_to(outside, target_is_directory=True)
                    else:
                        artifact.mkdir()
                        if replacement == "nonempty":
                            (artifact / "new").write_text("protected")
                    swapped.append(name)
                return real_rmdir(name, dir_fd=dir_fd)

            ui = mock.Mock()
            ui.is_interactive.return_value = True
            ui.select.side_effect = lambda choices: tuple(
                choice.id for choice in choices
            )
            ui.confirm_exact.return_value = True
            with (
                self.subTest(replacement=replacement),
                mock.patch.object(
                    removal.os, "rmdir", side_effect=replace_at_final_rmdir
                ) as patched,
                mock.patch.object(
                    os, "supports_dir_fd", os.supports_dir_fd | {patched}
                ),
            ):
                report = purge_space(PurgeRequest(paths=(project,)), ui=ui)
                self.assertEqual(len(swapped), 1)
                self.assertEqual(report.exit_code, 0)
                self.assertTrue(artifact.exists())
                self.assertEqual((outside / "sentinel").read_text(), "protected")
                if replacement == "nonempty":
                    self.assertEqual((artifact / "new").read_text(), "protected")

    def test_purge_keyboard_interrupt_at_selection_or_confirmation_exits_130(self):
        project = self.root / "project"
        project.mkdir()
        (project / "package.json").write_text("{}")
        artifact = project / "node_modules"
        artifact.mkdir()
        (artifact / "payload").write_text("preserved")
        for stage in ("select", "confirm_exact"):
            with self.subTest(stage=stage):
                ui = mock.Mock()
                ui.is_interactive.return_value = True
                ui.select.side_effect = lambda choices: tuple(
                    choice.id for choice in choices
                )
                getattr(ui, stage).side_effect = KeyboardInterrupt
                with mock.patch(
                    "hagency_cli.commands.purge_ui.QuestionaryPurgeUI", return_value=ui
                ):
                    result = CliRunner().invoke(
                        cli.app, ["file", "purge", str(project)]
                    )
                self.assertEqual(result.exit_code, 130)
                self.assertEqual((artifact / "payload").read_text(), "preserved")

    @unittest.skipUnless(
        os.rename in os.supports_dir_fd, "requires POSIX fd-based removal"
    )
    def test_purge_quarantine_rechecks_identity_and_reports_retained_data(self):
        real_rename, real_rmdir = os.rename, os.rmdir
        for stage in ("rename", "rmdir"):
            project = self.root / stage
            project.mkdir()
            (project / "package.json").write_text("{}")
            artifact = project / "node_modules"
            artifact.mkdir()
            (artifact / "old").write_text("rebuildable")
            quarantine_paths = []

            def replace_before_claim(source, destination, **kwargs):
                if source == "node_modules":
                    real_rename(artifact, project / "original-empty")
                    artifact.mkdir()
                    (artifact / "new").write_text("retained")
                    quarantine_paths.append(project / destination)
                return real_rename(source, destination, **kwargs)

            def fail_final_removal(name, *, dir_fd=None):
                if str(name).startswith(".hagency-purge-"):
                    quarantine_paths.append(project / name)
                    (project / name / "new").write_text("retained")
                    raise PermissionError("injected final removal failure")
                return real_rmdir(name, dir_fd=dir_fd)

            ui = mock.Mock()
            ui.is_interactive.return_value = True
            ui.select.side_effect = lambda choices: tuple(
                choice.id for choice in choices
            )
            ui.confirm_exact.return_value = True
            with self.subTest(stage=stage), contextlib.ExitStack() as stack:
                patched = stack.enter_context(
                    mock.patch.object(
                        removal.os,
                        stage,
                        side_effect=replace_before_claim
                        if stage == "rename"
                        else fail_final_removal,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        os, "supports_dir_fd", os.supports_dir_fd | {patched}
                    )
                )
                report = purge_space(PurgeRequest(paths=(project,)), ui=ui)
                self.assertEqual(report.exit_code, 1)
                self.assertEqual(len(quarantine_paths), 1)
                self.assertEqual((quarantine_paths[0] / "new").read_text(), "retained")
                self.assertIn(str(quarantine_paths[0]), report.results[0].message)
                if stage == "rename":
                    self.assertIn("identity changed", report.results[0].message)


if __name__ == "__main__":
    unittest.main()
