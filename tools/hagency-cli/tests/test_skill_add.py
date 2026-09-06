from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

from hagency_cli import cli
from hagency_cli.commands import skill_ui
from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import SourceNotReadyError, WorkspaceError
from hagency_cli.workspace.operations import skills as operations
from hagency_cli.workspace.source_inputs import classify_skill_input, remote_identity
from hagency_cli.workspace.sources import Source, resolve_sources


class FakeSelectionUI:
    def __init__(self, *, interactive=True, selected=None, conflict=None):
        self.interactive = interactive
        self.selected = selected
        self.conflict = conflict
        self.offered = ()
        self.conflicts = []

    def is_interactive(self):
        return self.interactive

    def choose_skills(self, candidates):
        self.offered = candidates
        return self.selected

    def select(self, name, candidates):
        self.conflicts.append((name, candidates))
        return self.conflict


class SkillAddTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "workspace"
        self.cwd = self.base / "consumer"
        self.root.mkdir()
        self.cwd.mkdir()
        self.config = self.root / "hagency-config.toml"
        self.registry = {
            "defaults": {"checkout_dir": "checkouts", "depth": 1},
            "source": {},
        }
        self.save()

    def save(self):
        write_toml(self.config, self.registry)

    def skill(self, root, name, content="initial"):
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {path.name}\ndescription: fixture\n---\n{content}\n",
            encoding="utf-8",
        )
        return path

    def git(self, root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def origin(self, name="repo", names=("one",)):
        root = self.base / name
        root.mkdir()
        self.git(root, "init", "-b", "main")
        self.git(root, "config", "user.name", "Fixture")
        self.git(root, "config", "user.email", "fixture@example.invalid")
        for skill in names:
            self.skill(root, skill)
        self.git(root, "add", ".")
        self.git(root, "commit", "-m", "initial")
        return root

    def add(self, value, **kwargs):
        return operations.add_skills(
            skill=value, root_value=str(self.root), cwd=self.cwd, **kwargs
        )

    def run_cli(self, *args, expected=0, env=None):
        with contextlib.chdir(self.cwd):
            result = CliRunner().invoke(
                cli.app, list(args), prog_name="hgc", catch_exceptions=False, env=env
            )
        self.assertEqual(result.exit_code, expected, result.stderr)
        return result

    def test_url_registers_clones_and_links_then_reuses_without_update(self):
        origin = self.origin()
        report = self.add(origin.as_uri())
        self.assertTrue(report.registered)
        checkout = self.root / "checkouts" / "repo"
        installed = self.cwd / ".agents" / "skills" / "one"
        self.assertEqual(installed.resolve(), checkout / "one")
        self.assertEqual(
            self.git(checkout, "remote", "get-url", "origin"), origin.as_uri()
        )
        self.skill(origin, "one", "updated")
        self.git(origin, "add", ".")
        self.git(origin, "commit", "-m", "upstream update")
        before = self.config.read_bytes()
        with mock.patch.object(
            operations, "sync_source", side_effect=AssertionError("must not update")
        ):
            second = self.add(origin.as_uri())
        self.assertFalse(second.registered)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIn("initial", (installed / "SKILL.md").read_text())
        self.run_cli("source", "sync", "repo", "-r", str(self.root))
        self.assertIn("updated", (installed / "SKILL.md").read_text())

    def test_source_add_sync_accepts_a_local_git_address_with_spaces(self):
        origin = self.origin(name="local repo")
        self.run_cli(
            "source",
            "add",
            "on-disk",
            "--url",
            str(origin),
            "--sync",
            "-r",
            str(self.root),
        )
        self.assertTrue((self.root / "checkouts/on-disk/one/SKILL.md").is_file())

    def test_registered_missing_source_and_exact_selector_are_obtained(self):
        origin = self.origin(names=("one", "two"))
        self.registry["source"]["named"] = {"remote": {"url": origin.as_uri()}}
        self.save()
        report = self.add("named:two")
        self.assertFalse(report.registered)
        self.assertEqual([c.name for c in report.selected], ["two"])
        self.assertTrue((self.root / "checkouts" / "named" / ".git").is_dir())

    def test_github_shorthand_reuses_unique_normalized_url_without_network(self):
        local = self.base / "shared"
        self.skill(local, "one")
        self.registry["source"]["custom"] = {
            "path": str(local),
            "remote": {"url": "https://github.com/acme/repo/"},
        }
        self.save()
        with mock.patch.object(
            operations, "sync_source", side_effect=AssertionError("network")
        ):
            result = self.add("acme/repo")
        self.assertEqual(result.source.name, "custom")
        self.assertFalse(result.registered)
        self.assertEqual(set(read_toml(self.config)["source"]), {"custom"})
        self.assertEqual(
            remote_identity("git@github.com:a/b.git/"), "git@github.com:a/b"
        )
        self.assertNotEqual(
            remote_identity("https://github.com/a/b"),
            remote_identity("git@github.com:a/b"),
        )
        self.assertNotEqual(
            remote_identity("https://other.invalid/a/b.git"),
            remote_identity("https://other.invalid/a/b"),
        )

    def test_input_precedence_and_cross_platform_paths(self):
        names = {n: Source(n, self.root, None) for n in ("acme/repo", "x", "C")}
        cases = {
            "acme/repo": "source",
            "acme/repo:nested/one": "selector",
            "x:one": "selector",
            "C:/work/repo": "path",
            r"C:\work\repo": "path",
            r"\\server\share\repo": "path",
            "../repo": "path",
            "./repo": "path",
            "~/repo": "path",
            "/tmp/repo": "path",
            "acme/new": "url",
            "one": "name",
            "git@github.com:acme/new.git": "url",
        }
        for value, kind in cases.items():
            with self.subTest(value=value):
                self.assertEqual(classify_skill_input(value, names).kind, kind)

    def test_relative_local_input_registers_absolute_path_and_links(self):
        source = self.base / "local"
        skill = self.skill(source, "one")
        report = self.add("../local")
        self.assertTrue(report.registered)
        self.assertEqual(read_toml(self.config)["source"]["local"]["path"], str(source))
        self.assertEqual((self.cwd / ".agents/skills/one").resolve(), skill)

    def test_local_subtree_uses_most_specific_source_and_limits_discovery(self):
        outer = self.base / "catalog"
        inner = outer / "nested"
        target = self.skill(inner, "one")
        self.skill(outer, "other")
        self.registry["source"] = {
            "outer": {"path": str(outer)},
            "inner": {"path": str(inner)},
        }
        self.save()
        result = self.add(str(inner))
        self.assertEqual(result.source.name, "inner")
        self.assertEqual([s.target for s in result.selected], [target])
        self.assertFalse((self.cwd / ".agents/skills/other").exists())
        before = self.config.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, "outside the input directory"):
            self.add(str(inner), source_name="outer", selectors=("other",))
        self.assertEqual(self.config.read_bytes(), before)

    def test_workspace_local_input_uses_implicit_source(self):
        target = self.skill(self.root / "skills", "one")
        result = self.add(str(target))
        self.assertEqual(result.source.name, "workspace")
        self.assertFalse(result.registered)
        self.assertNotIn("source", read_toml(self.config))

    def test_local_tie_requires_source_name(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.registry["source"] = {
            "a": {"path": str(source)},
            "b": {"path": str(source)},
        }
        self.save()
        before = self.config.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, "multiple sources contain"):
            self.add(str(source))
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.add(str(source), source_name="b").source.name, "b")

    def test_same_url_multiple_registrations_requires_name(self):
        source = self.base / "local"
        self.skill(source, "one")
        url = "https://github.com/acme/repo.git"
        self.registry["source"] = {
            name: {"path": str(source), "remote": {"url": url}} for name in ("a", "b")
        }
        self.save()
        with self.assertRaisesRegex(WorkspaceError, "multiple sources match"):
            self.add(url)
        self.assertEqual(self.add(url, source_name="b").source.name, "b")

    def test_explicit_source_never_retargets_url_or_ref(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.registry["source"] = {
            "a": {
                "path": str(source),
                "remote": {"url": "https://github.com/acme/repo", "ref": "main"},
            }
        }
        self.save()
        before = self.config.read_bytes()
        for value, kwargs in [
            ("https://github.com/other/repo", {"source_name": "a"}),
            ("a", {"ref": "feature"}),
            ("https://github.com/acme/repo", {"source_name": "a", "ref": "feature"}),
        ]:
            with (
                self.subTest(value=value, kwargs=kwargs),
                self.assertRaisesRegex(WorkspaceError, "never retargeted"),
            ):
                self.add(value, **kwargs)
        self.assertEqual(self.config.read_bytes(), before)

    def test_actual_checkout_ref_is_verified_without_switching(self):
        origin = self.origin()
        result = self.add(origin.as_uri(), ref="main")
        self.git(result.source.path, "checkout", "-b", "local-work")
        with self.assertRaisesRegex(SourceNotReadyError, "not checked out") as error:
            self.add(origin.as_uri(), ref="main")
        self.assertEqual(error.exception.sources, (result.source.name,))
        result_cli = self.run_cli(
            "skill",
            "add",
            origin.as_uri(),
            "--ref",
            "main",
            "-r",
            str(self.root),
            expected=1,
        )
        self.assertIn("run: hgc source sync repo", result_cli.stderr)
        self.assertEqual(
            self.git(result.source.path, "branch", "--show-current"), "local-work"
        )

    def test_tag_ref_can_install_detached_checkout(self):
        origin = self.origin()
        self.git(origin, "tag", "v1")
        result = self.add(origin.as_uri(), ref="v1")
        self.assertEqual([c.name for c in result.selected], ["one"])
        self.assertEqual(
            self.git(result.source.path, "rev-parse", "HEAD"),
            self.git(origin, "rev-parse", "v1"),
        )

    def test_local_subdirectory_of_remote_checkout_accepts_matching_ref(self):
        origin = self.origin()
        first = self.add(origin.as_uri())
        before = self.config.read_bytes()
        with mock.patch.object(
            operations, "sync_source", side_effect=AssertionError("must not fetch")
        ):
            report = self.add(str(first.source.path / "one"), ref="main")
        self.assertEqual(report.source, first.source)
        self.assertEqual([candidate.name for candidate in report.selected], ["one"])
        self.assertEqual(self.config.read_bytes(), before)
        with self.assertRaisesRegex(WorkspaceError, "never retargeted"):
            self.add(str(first.source.path / "one"), ref="other")
        local = self.base / "local-only"
        self.skill(local, "one")
        with self.assertRaisesRegex(WorkspaceError, "remote source"):
            self.add(str(local), ref="main")
        self.assertEqual(self.config.read_bytes(), before)

    def test_existing_checkout_dry_run_verifies_ref_without_fetch_or_writes(self):
        origin = self.origin()
        installed = self.add(origin.as_uri())
        before = self.config.read_bytes()
        head = (installed.source.path / ".git/HEAD").read_bytes()
        target = self.base / "dry-target"
        with mock.patch.object(
            operations, "sync_source", side_effect=AssertionError("must not fetch")
        ):
            report = self.add(
                origin.as_uri(), ref="main", dry_run=True, skills_path=str(target)
            )
        self.assertFalse(report.provisional)
        self.assertEqual(len(report.selected), 1)
        self.assertFalse(target.exists())
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual((installed.source.path / ".git/HEAD").read_bytes(), head)

    def test_checkout_override_is_persisted_for_new_source(self):
        origin = self.origin()
        result = self.add(origin.as_uri(), checkout_dir="alternate")
        expected = self.root / "alternate" / "repo"
        self.assertEqual(result.source.path, expected)
        self.assertEqual(
            read_toml(self.config)["source"]["repo"]["path"], str(expected)
        )
        resolved = resolve_sources(
            read_toml(self.config), repo_root=self.root, checkout_override=None
        )
        self.assertEqual(resolved["repo"].path, expected)

    def test_defaults_and_existing_checkout_override_do_not_rewrite_registration(self):
        origin = self.origin()
        self.git(origin, "checkout", "-b", "stable")
        for revision in ("second", "third"):
            self.skill(origin, "one", revision)
            self.git(origin, "add", ".")
            self.git(origin, "commit", "-m", revision)
        self.registry["defaults"].update(remote_ref="stable", depth=2)
        self.save()
        first = self.add(origin.as_uri())
        self.assertEqual(
            self.git(first.source.path, "branch", "--show-current"), "stable"
        )
        self.assertEqual(
            self.git(first.source.path, "rev-list", "--count", "HEAD"), "2"
        )
        before = self.config.read_bytes()
        second = self.add(origin.as_uri(), checkout_dir="temporary-checkouts")
        self.assertFalse(second.registered)
        self.assertEqual(second.source.path, self.root / "temporary-checkouts/repo")
        self.assertTrue((second.source.path / ".git").is_dir())
        self.assertEqual(self.config.read_bytes(), before)
        normal = resolve_sources(
            read_toml(self.config), repo_root=self.root, checkout_override=None
        )
        self.assertEqual(normal["repo"].path, first.source.path)

    def test_explicit_ref_disambiguates_same_url_without_updating_either_source(self):
        origin = self.origin()
        self.git(origin, "branch", "feature")
        self.add(origin.as_uri(), source_name="primary", ref="main")
        self.add(origin.as_uri(), source_name="feature", ref="feature")
        with self.assertRaisesRegex(WorkspaceError, "multiple sources match"):
            self.add(origin.as_uri())
        before = self.config.read_bytes()
        with mock.patch.object(
            operations, "sync_source", side_effect=AssertionError("no update")
        ):
            result = self.add(origin.as_uri(), ref="feature")
        self.assertEqual(result.source.name, "feature")
        self.assertFalse(result.registered)
        self.assertEqual(self.config.read_bytes(), before)

    def test_missing_local_source_is_not_obtained_as_a_remote(self):
        self.registry["source"]["local"] = {"path": str(self.base / "absent")}
        self.save()
        with mock.patch.object(operations, "sync_source") as fetch:
            with self.assertRaisesRegex(WorkspaceError, "source path does not exist"):
                self.add("local")
        fetch.assert_not_called()
        self.assertFalse((self.cwd / ".agents").exists())

    def test_remote_name_collision_uses_owner_then_requires_explicit_name(self):
        self.registry["source"]["repo"] = {"path": str(self.root)}
        self.save()
        result = self.add("acme/repo", dry_run=True)
        self.assertEqual(result.source.name, "acme/repo")
        self.registry["source"]["acme/repo"] = {"path": str(self.root)}
        self.save()
        # Exact registered source names take precedence over shorthand; URL selects a new source.
        with self.assertRaisesRegex(WorkspaceError, "source already exists"):
            self.add("https://github.com/acme/repo", dry_run=True)
        result = self.add(
            "https://github.com/acme/repo", source_name="other", dry_run=True
        )
        self.assertEqual(result.source.name, "other")

    def test_occupied_unregistered_checkout_fails_before_registration(self):
        occupied = self.root / "checkouts/repo"
        self.skill(occupied, "unrelated")
        before = self.config.read_bytes()
        with (
            mock.patch.object(operations, "sync_source") as fetch,
            self.assertRaisesRegex(WorkspaceError, "occupied"),
        ):
            self.add("acme/repo")
        fetch.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.cwd / ".agents").exists())
        self.assertTrue((occupied / "unrelated/SKILL.md").is_file())

    def test_invalid_options_paths_and_names_do_not_register(self):
        before = self.config.read_bytes()
        cases = [
            ("acme/repo", {"selectors": ("one",), "all_skills": True}),
            ("../missing", {}),
            ("acme/repo", {"source_name": "workspace"}),
            ("acme/repo", {"source_name": "../escape"}),
            ("acme/repo", {"source_name": "a:b"}),
            ("acme/repo", {"source_name": "/absolute"}),
            ("acme/repo", {"ref": "--bad"}),
            ("https://github.com/acme/repo/tree/main/skills", {}),
            ("https://[invalid/repo", {}),
            ("https://example.invalid:invalid/repo", {}),
            ("https://github.com/acme/repo\n", {"source_name": "safe"}),
            ("acme/repo", {"selectors": ("",)}),
            ("acme/repo", {"skills_path": "skills", "global_install": True}),
        ]
        for value, kwargs in cases:
            with (
                self.subTest(value=value, kwargs=kwargs),
                self.assertRaises(WorkspaceError),
            ):
                self.add(value, **kwargs)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.root / "checkouts").exists())
        self.assertFalse((self.cwd / ".agents").exists())

    def test_invalid_config_is_rejected_before_registration_or_fetch(self):
        for content in (
            "defaults = 'invalid'\n",
            "source = []\n",
            "[source]\ninvalid = 'string'\n",
            "[source.invalid]\npath = 42\n",
            "[source.invalid]\nremote = 'string'\n",
            "[source.invalid.remote]\nurl = 42\n",
            "[defaults]\ncheckout_dir = false\n",
            "[defaults]\ncheckout_dir = 'checkouts'\ndepth = 0\n",
        ):
            with (
                self.subTest(content=content),
                mock.patch.object(operations, "sync_source") as fetch,
            ):
                self.config.write_text(content)
                result = self.run_cli(
                    "skill", "add", "acme/repo", "-r", str(self.root), expected=1
                )
                self.assertIn("Error:", result.stderr)
                self.assertEqual(self.config.read_text(), content)
                fetch.assert_not_called()

    def test_clone_failure_preserves_registration_and_partial_checkout(self):
        def failed(source, **kwargs):
            source.path.mkdir(parents=True)
            (source.path / "partial").write_text("keep")
            raise subprocess.CalledProcessError(128, ["git", "clone"])

        with (
            mock.patch.object(operations, "sync_source", side_effect=failed),
            self.assertRaisesRegex(WorkspaceError, "retained"),
        ):
            self.add("acme/repo")
        self.assertIn("repo", read_toml(self.config)["source"])
        self.assertEqual((self.root / "checkouts/repo/partial").read_text(), "keep")
        self.assertFalse((self.cwd / ".agents").exists())

    def test_non_tty_multiskill_requires_selection_and_retains_source(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.skill(source, "two")
        with self.assertRaisesRegex(WorkspaceError, "--skill.*--all"):
            self.add(str(source), ui=FakeSelectionUI(interactive=False))
        self.assertIn("local", read_toml(self.config)["source"])
        self.assertFalse((self.cwd / ".agents").exists())
        result = self.add("local", selectors=("one", "two"))
        self.assertEqual({s.name for s in result.selected}, {"one", "two"})

    def test_tty_multiselect_installs_only_chosen_skills(self):
        source = self.base / "local"
        one = self.skill(source, "one")
        two = self.skill(source, "two")
        ui = FakeSelectionUI(selected=(two,))
        report = self.add(str(source), ui=ui)
        self.assertEqual({s.target for s in ui.offered}, {one, two})
        self.assertEqual([s.name for s in report.selected], ["two"])
        self.assertFalse((self.cwd / ".agents/skills/one").exists())

    def test_cancelled_or_invalid_selection_never_installs(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.skill(source, "two")
        for selected in (None, (), (self.base / "outside",)):
            with self.subTest(selected=selected), self.assertRaises(WorkspaceError):
                self.add(str(source), ui=FakeSelectionUI(selected=selected))
            self.assertFalse((self.cwd / ".agents").exists())
            self.assertIn("local", read_toml(self.config)["source"])

    def test_all_does_not_bypass_duplicate_names_and_resolves_before_writes(self):
        source = self.base / "local"
        self.skill(source, "safe")
        a = self.skill(source, "a/duplicate")
        b = self.skill(source, "b/duplicate")
        with self.assertRaisesRegex(WorkspaceError, "duplicate discovered"):
            self.add(
                str(source), all_skills=True, ui=FakeSelectionUI(interactive=False)
            )
        self.assertFalse((self.cwd / ".agents").exists())
        ui = FakeSelectionUI(conflict=b)
        result = self.add("local", all_skills=True, ui=ui)
        self.assertEqual(len(ui.conflicts), 1)
        self.assertEqual({c.target for c in result.selected}, {source / "safe", b})
        self.assertNotEqual((self.cwd / ".agents/skills/duplicate").resolve(), a)

    def test_all_selectors_validated_before_any_install(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.skill(source, "two")
        with self.assertRaisesRegex(WorkspaceError, "no candidates"):
            self.add(str(source), selectors=("one", "missing"))
        self.assertFalse((self.cwd / ".agents").exists())
        report = self.add("local", selectors=("one", "one"))
        self.assertEqual(len(report.selected), 1)

    def test_exact_reference_rejects_additional_selection_before_writes(self):
        self.skill(self.root, "one")
        for value in ("one", "workspace:one"):
            for options in ({"all_skills": True}, {"selectors": ("one",)}):
                with (
                    self.subTest(value=value, options=options),
                    self.assertRaisesRegex(WorkspaceError, "exact skill reference"),
                ):
                    self.add(value, **options)
        self.assertFalse((self.cwd / ".agents").exists())

    def test_dry_run_remote_is_provisional_and_local_has_concrete_plan(self):
        before = self.config.read_bytes()
        events = []
        with mock.patch(
            "subprocess.run", side_effect=AssertionError("no network or subprocess")
        ):
            result = self.add(
                "acme/repo", selectors=("one",), dry_run=True, progress=events.append
            )
        self.assertTrue(result.provisional)
        self.assertIn("not yet verified", "\n".join(e.message for e in events))
        self.assertEqual(self.config.read_bytes(), before)
        source = self.base / "local"
        self.skill(source, "one")
        result = self.add(str(source), dry_run=True)
        self.assertFalse(result.provisional)
        self.assertEqual(len(result.selected), 1)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.root / "checkouts").exists())
        self.assertFalse((self.cwd / ".agents").exists())

    def test_dry_run_multiskill_does_not_prompt(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.skill(source, "two")
        ui = mock.Mock()
        result = self.add(str(source), dry_run=True, ui=ui)
        self.assertTrue(result.provisional)
        ui.choose_skills.assert_not_called()
        self.assertFalse((self.cwd / ".agents").exists())

    def test_cli_short_selectors_target_options_and_multi_value_normalizer(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.skill(source, "two")
        destination = self.base / "target"
        args = [
            "skill",
            "add",
            str(source),
            "-s",
            "one",
            "-s",
            "two",
            "-r",
            str(self.root),
            "-p",
            str(destination),
        ]
        with (
            contextlib.chdir(self.cwd),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(SystemExit) as stopped,
        ):
            cli.main(args)
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual({p.name for p in destination.iterdir()}, {"one", "two"})
        self.run_cli(
            "skill",
            "add",
            "local",
            "--all",
            "--skill",
            "one",
            "-r",
            str(self.root),
            expected=2,
        )

    def test_cli_multiselect_adapter_is_not_constructed_with_preselected_skills(self):
        candidates = (
            operations.SkillLinkCandidate("one", "source", self.base / "one"),
        )
        with mock.patch.object(skill_ui.questionary, "checkbox") as checkbox:
            checkbox.return_value.unsafe_ask.return_value = [candidates[0].target]
            selected = skill_ui.QuestionarySkillConflictUI().choose_skills(candidates)
        self.assertEqual(selected, (candidates[0].target,))
        self.assertFalse(checkbox.call_args.kwargs["choices"][0].checked)

    def test_completion_lists_sources_and_source_selectors_without_fetching(self):
        source = self.base / "local"
        self.skill(source, "one")
        self.registry["source"]["named"] = {"path": str(source)}
        self.save()
        for words, index, expected in [
            (f"hgc skill add -r {self.root} ", 5, "named"),
            (f"hgc skill add named -r {self.root} --skill ", 7, "one"),
        ]:
            with mock.patch(
                "subprocess.run", side_effect=AssertionError("completion is read-only")
            ):
                result = self.run_cli(
                    env={
                        "_HGC_COMPLETE": "complete_bash",
                        "COMP_WORDS": words,
                        "COMP_CWORD": str(index),
                    }
                )
            self.assertIn(expected, result.stdout.splitlines())
            self.assertEqual(result.stderr, "")

    def test_missing_workspace_and_invalid_config_are_clean_errors(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, "missing workspace config"):
            operations.add_skills(
                skill="acme/repo", root_value=str(self.base / "missing"), cwd=self.cwd
            )
        self.assertEqual(self.config.read_bytes(), before)
        self.config.write_text("invalid = [")
        self.run_cli("skill", "add", "acme/repo", "-r", str(self.root), expected=1)
        self.assertFalse((self.root / "checkouts").exists())
