from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hagency_cli import cli
from hagency_cli.commands import skill_ui
from hagency_cli.files.sync.models import SyncDirection
from hagency_cli.files.sync.operations import sync_workspace_files
from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import WorkspaceError
from hagency_cli.workspace.operations import skills as skill_operations
from hagency_cli.workspace.profiles import apply_profile
from hagency_cli.workspace.skills import resolve_selector
from hagency_cli.workspace.sources import Source
from typer.testing import CliRunner


if __package__:
    from .support import LocalSFTPRemote
else:
    from support import LocalSFTPRemote


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = self.root / "hagency-config.toml"
        self.registry = {
            "defaults": {"checkout_dir": "checkouts", "depth": 1},
            "source": {"local": {"path": str(self.source)}},
        }
        write_toml(self.config, self.registry)
        self.destination = self.base / "installed"

    def skill(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text("---\nname: fixture\ndescription: test\n---\n")
        return path

    def invoke(self, *args, expected=0):
        with contextlib.chdir(self.root):
            result = CliRunner().invoke(cli.app, list(args), catch_exceptions=False)
        self.assertEqual(result.exit_code, expected, result.stderr)
        return result

    def test_selector_rejects_parent_absolute_and_symlink_escapes(self):
        outside = self.skill(self.root / "secret")
        (self.source / "escape").symlink_to(outside, target_is_directory=True)
        source = Source("local", self.source, None)
        for selector in (
            "../secret",
            str(outside),
            "escape",
            r"..\secret",
            "C:/secret",
            r"\\server\share\secret",
        ):
            with self.subTest(selector=selector):
                with self.assertRaisesRegex(WorkspaceError, "outside|relative"):
                    resolve_selector(source, selector)

    def test_selectors_keep_nested_and_internal_symlink_paths_working(self):
        inside = self.skill(self.source / "nested/inside")
        (self.source / "alias").symlink_to(inside, target_is_directory=True)
        source = Source("local", self.source, None)
        for selector in ("inside", "nested/inside", "./nested/inside", "alias"):
            with self.subTest(selector=selector):
                self.assertEqual(
                    resolve_selector(source, selector)[0][1].resolve(), inside
                )

    def test_exact_and_filtered_install_reject_escapes_without_target_writes(self):
        self.skill(self.root / "secret")
        before = self.config.read_bytes()
        for args in (("local:../secret",), ("local", "--skill", "../secret")):
            with self.subTest(args=args):
                self.invoke(
                    "skill", "add", *args, "-p", str(self.destination), expected=1
                )
                self.assertFalse(self.destination.exists())
                self.assertEqual(self.config.read_bytes(), before)

    def test_profile_mutations_and_apply_reject_outside_selectors(self):
        self.skill(self.root / "secret")
        self.skill(self.source / "inside")
        for option in ("--include", "--exclude"):
            with self.subTest(option=option):
                self.invoke(
                    "profile",
                    "add",
                    "bad",
                    "-AS",
                    "local",
                    option,
                    "../secret",
                    expected=1,
                )
                self.assertFalse((self.root / "profiles/bad").exists())
        self.invoke("profile", "add", "valid", "-AS", "local")
        profile_path = self.root / "profiles/valid/config.toml"
        before = profile_path.read_bytes()
        self.invoke(
            "profile",
            "update",
            "valid",
            "-AS",
            "local",
            "--include",
            "../secret",
            expected=1,
        )
        self.assertEqual(profile_path.read_bytes(), before)
        write_toml(
            profile_path,
            {"name": "valid", "skill": {"local": {"include": ["../secret"]}}},
        )
        self.invoke(
            "profile", "apply", "valid", "-p", str(self.destination), expected=1
        )
        self.assertFalse(self.destination.exists())

    def test_static_selector_escape_fails_before_registration_or_acquisition(self):
        before = self.config.read_bytes()
        with mock.patch.object(skill_operations, "sync_source") as fetch:
            for value, selectors in (
                ("acme/repo", ("../secret",)),
                ("acme/repo", ("C:/secret",)),
            ):
                with self.subTest(value=value, selectors=selectors):
                    with self.assertRaisesRegex(WorkspaceError, "outside|relative"):
                        skill_operations.add_skills(
                            skill=value,
                            selectors=selectors,
                            root_value=str(self.root),
                            cwd=self.base,
                        )
                    self.assertEqual(self.config.read_bytes(), before)
            fetch.assert_not_called()

    def test_missing_checkout_rejects_exact_and_profile_escapes_before_fetch(self):
        self.registry["source"]["remote"] = {
            "remote": {"url": "https://github.com/acme/repo.git"}
        }
        write_toml(self.config, self.registry)
        before = self.config.read_bytes()
        with mock.patch.object(skill_operations, "sync_source") as fetch:
            self.invoke(
                "skill",
                "add",
                "remote:../secret",
                "-p",
                str(self.destination),
                expected=1,
            )
            fetch.assert_not_called()
        self.invoke(
            "profile", "add", "bad", "-AS", "remote", "-i", "../secret", expected=1
        )
        self.assertFalse((self.root / "profiles/bad").exists())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.config.read_bytes(), before)

    def test_profile_and_skill_dry_run_never_prompt_for_conflicts_in_tty(self):
        self.skill(self.source / "a/same")
        self.skill(self.source / "b/same")
        source = Source("local", self.source, None)
        ui = mock.Mock()
        ui.is_interactive.return_value = True
        events = []
        apply_profile(
            {"skill": {"local": {}}},
            {"local": source},
            self.root,
            self.destination,
            link_mode="symlink",
            dry_run=True,
            conflict_ui=ui,
            progress=events.append,
        )
        ui.select.assert_not_called()
        self.assertIn("conflict", "\n".join(event.message for event in events))
        with mock.patch.object(skill_ui, "QuestionarySkillConflictUI", return_value=ui):
            self.invoke(
                "skill",
                "add",
                "local",
                "--all",
                "--dry-run",
                "-p",
                str(self.destination),
            )
        ui.select.assert_not_called()
        self.assertFalse(self.destination.exists())

    def test_source_add_rejects_malformed_urls_before_persistence(self):
        before = self.config.read_bytes()
        for url in (
            "https://[invalid",
            "https://example.invalid:bad/repo",
            "https://github.com/a/b?query=1",
            "https://github.com/a/b\n",
        ):
            for args in ((url,), ("new", "--url", url)):
                with self.subTest(args=args):
                    self.invoke("source", "add", *args, expected=1)
                    self.assertEqual(self.config.read_bytes(), before)

    def test_source_add_preserves_local_git_addresses_and_scp_transport(self):
        for name, url in (
            ("on-disk", str(self.base / "repo with spaces")),
            ("scp", "git@example.invalid:owner/repo.git"),
        ):
            self.invoke("source", "add", name, "--url", url)
            self.assertEqual(
                read_toml(self.config)["source"][name]["remote"]["url"], url
            )

    def test_config_sync_protects_git_metadata_in_both_trees(self):
        for direction in SyncDirection:
            for ignores in ([], ["!.git", "!**/.git/**", "!**/.GIT/**"]):
                with self.subTest(direction=direction, ignores=ignores):
                    case = self.base / f"{direction.value}-{len(ignores)}"
                    project, store = case / "project", case / "server"
                    (project / ".vscode").mkdir(parents=True)
                    remote_root = store / "remote"
                    remote_root.mkdir(parents=True)
                    (project / ".vscode/sftp.json").write_text(
                        json.dumps(
                            {
                                "host": "fixture.invalid",
                                "remotePath": "/remote",
                                "username": "test",
                                "ignore": ignores,
                                "syncOption": {"delete": True},
                            }
                        )
                    )
                    paths = (
                        ".git/config",
                        "nested/.GIT/config",
                        "worktree/.git",
                        "only-local/.git/config",
                    )
                    for relative in paths:
                        local = project / relative
                        local.parent.mkdir(parents=True, exist_ok=True)
                        local.write_bytes(b"local metadata")
                        if relative.startswith("only-local/"):
                            continue
                        remote = remote_root / relative
                        remote.parent.mkdir(parents=True, exist_ok=True)
                        remote.write_bytes(b"remote metadata has a different size")
                    remote_only = remote_root / "only-remote/.git/config"
                    remote_only.parent.mkdir(parents=True)
                    remote_only.write_bytes(b"remote only")
                    report = sync_workspace_files(
                        project,
                        direction,
                        remote_factory=lambda config: LocalSFTPRemote(config, store),
                    )
                    self.assertFalse(
                        any(
                            ".git" in [p.casefold() for p in action.path.parts]
                            for action in report.actions
                        )
                    )
                    for relative in paths:
                        self.assertEqual(
                            (project / relative).read_bytes(), b"local metadata"
                        )
                        if not relative.startswith("only-local/"):
                            self.assertEqual(
                                (remote_root / relative).read_bytes(),
                                b"remote metadata has a different size",
                            )
                    self.assertEqual(remote_only.read_bytes(), b"remote only")
