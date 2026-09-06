from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import tomllib

from typer.testing import CliRunner

import hagency_cli.commands.file as commands_file_module
import hagency_cli.commands.purge_render as commands_purge_render_module
import hagency_cli.commands.purge_ui as commands_purge_ui_module
import hagency_cli.commands.service as commands_service_module
import hagency_cli.commands.skill_ui as commands_skill_ui_module
import hagency_cli.files.purge.models as files_purge_models_module
import hagency_cli.paths as paths_module
import hagency_cli.workspace.discovery as workspace_discovery_module
import hagency_cli.workspace.git as workspace_git_module
import hagency_cli.workspace.operations.sources as workspace_operations_sources_module
import hagency_cli.workspace.skills as workspace_skills_module
import hagency_cli.workspace.sources as workspace_sources_module
from hagency_cli import cli
from hagency_cli.commands.shared import render_event
from hagency_cli.workspace.errors import WorkspaceError


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "content").mkdir(parents=True)
        self.config_path = self.root / "hagency-config.toml"
        self.config_path.write_text(
            textwrap.dedent(
                """
                [defaults]
                checkout_dir = "checkouts"

                [source.local-source]
                path = "local-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.write_skill(self.root / "skills" / "local-one")
        self.write_skill(self.root / "local-source" / "nested" / "external-one")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write_skill(self, path: Path, name: str | None = None) -> None:
        path.mkdir(parents=True, exist_ok=True)
        skill_name = name or path.name
        (path / "SKILL.md").write_text(
            textwrap.dedent(
                f"""
                ---
                name: {skill_name}
                description: Test skill.
                ---

                Test body.
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def run_cli(
        self,
        *args: str,
        cwd: Path | None = None,
        expected: int = 0,
        color: bool = False,
    ) -> tuple[str, str]:
        old_cwd = Path.cwd()
        try:
            os.chdir(cwd or self.root)
            result = CliRunner().invoke(
                cli.app,
                cli.normalize_legacy_multi_value_options(args),
                prog_name="hgc",
                color=color,
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)
        self.assertEqual(result.exit_code, expected, result.stderr)
        return result.stdout, result.stderr

    def complete_bash(
        self, words: str, cword: int, *, cwd: Path | None = None
    ) -> tuple[list[str], str]:
        old_cwd = Path.cwd()
        try:
            os.chdir(cwd or self.root)
            result = CliRunner().invoke(
                cli.app,
                [],
                prog_name="hgc",
                color=False,
                catch_exceptions=False,
                env={
                    "_HGC_COMPLETE": "complete_bash",
                    "COMP_WORDS": words,
                    "COMP_CWORD": str(cword),
                },
            )
        finally:
            os.chdir(old_cwd)
        self.assertEqual(result.exit_code, 0, result.stderr)
        return [line for line in result.stdout.splitlines() if line], result.stderr

    def run_main(
        self, *args: str, cwd: Path | None = None, expected: int = 0
    ) -> tuple[str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_cwd = Path.cwd()
        code = 0
        try:
            os.chdir(cwd or self.root)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    cli.main(args)
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def read_config(self) -> dict:
        with self.config_path.open("rb") as handle:
            return tomllib.load(handle)

    def read_profile(self, name: str = "content") -> dict:
        with (self.root / "profiles" / name / "config.toml").open("rb") as handle:
            return tomllib.load(handle)

    def append_remote_source(self, name: str = "remote-source") -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""

                    [source.{name}.remote]
                    url = "https://example.invalid/acme/ExamplePack.git"
                    """
                )
            )

    def append_local_source(self, name: str) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    f"""

                    [source.{name}]
                    path = "{name}"
                    """
                )
            )
        (self.root / name).mkdir()

    def run_git(self, cwd: Path | None, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.strip()

    def commit_file(self, repo: Path, content: str, message: str) -> str:
        (repo / "file.txt").write_text(content, encoding="utf-8")
        self.run_git(repo, "add", "file.txt")
        self.run_git(
            repo,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            message,
        )
        return self.run_git(repo, "rev-parse", "HEAD")

    def write_remote_source_config(
        self, name: str, origin: Path, *, depth: int | None = 1
    ) -> None:
        self.write_remote_sources_config({name: origin}, depth=depth)

    def write_remote_sources_config(
        self, origins: dict[str, Path], *, depth: int | None = 1
    ) -> None:
        depth_line = f"depth = {depth}\n" if depth is not None else ""
        source_lines = "\n".join(
            textwrap.dedent(
                f"""
                [source.{name}.remote]
                url = "{origin.as_uri()}"
                """
            ).strip()
            for name, origin in origins.items()
        )
        self.config_path.write_text(
            textwrap.dedent(
                f"""
                [defaults]
                checkout_dir = "checkouts"
                {depth_line}
                {source_lines}
                """
            ).lstrip(),
            encoding="utf-8",
        )

    def create_remote_source_pair(
        self, name: str, *, shallow: bool
    ) -> tuple[Path, Path]:
        origin = self.root / f"{name}-origin"
        self.run_git(None, "init", "-b", "main", str(origin))
        self.commit_file(origin, "one", "one")

        checkout = self.root / "checkouts" / name
        checkout.parent.mkdir(parents=True, exist_ok=True)
        clone_args = ["clone", "--branch", "main"]
        if shallow:
            clone_args.extend(["--depth", "1"])
        clone_args.extend([origin.as_uri(), str(checkout)])
        self.run_git(None, *clone_args)
        return origin, checkout

    def create_remote_source_checkout(
        self, name: str = "remote-source", *, shallow: bool = True
    ) -> tuple[Path, Path]:
        origin, checkout = self.create_remote_source_pair(name, shallow=shallow)
        self.write_remote_source_config(name, origin, depth=1 if shallow else None)
        return origin, checkout

    def test_package_exposes_only_hgc_console_script(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]

        self.assertEqual(project["scripts"], {"hgc": "hagency_cli.cli:main"})
        self.assertIn("multidict>=6,<7", project["dependencies"])
        self.assertIn("questionary>=2.1,<3", project["dependencies"])

    def test_help_and_usage_errors_are_plain_text_with_completion_enabled(self) -> None:
        stdout, stderr = self.run_cli("--help", color=True)

        self.assertEqual(stderr, "")
        self.assertIn("--install-completion", stdout)
        self.assertIn("--show-completion", stdout)
        self.assertIn("Alias for source.", stdout)
        self.assertIn("Alias for profile.", stdout)

        _stdout, stderr = self.run_cli(expected=2, color=True)
        self.assertIn("Missing command", stderr)
        for output in (stdout, stderr):
            self.assertNotIn("\x1b[", output)
            self.assertNotIn("╭", output)
            self.assertNotIn("│", output)
            self.assertNotIn("╰", output)

    def test_short_help_option_is_available_at_every_command_level(self) -> None:
        for args in (
            ("-h",),
            ("file", "-h"),
            ("file", "init", "-h"),
            ("file", "pack", "-h"),
            ("file", "apply", "-h"),
            ("file", "sync", "-h"),
            ("service", "-h"),
            ("service", "model-proxy", "start", "-h"),
            ("file", "-h"),
            ("file", "purge", "-h"),
            ("source", "-h"),
            ("source", "show", "-h"),
            ("p", "apply", "-h"),
        ):
            with self.subTest(args=args):
                stdout, stderr = self.run_cli(*args)
                self.assertEqual(stderr, "")
                self.assertIn("Usage:", stdout)
                self.assertIn("-h, --help", stdout)

    def test_serve_model_proxy_resolves_workspace_config_and_validates_listen_address(
        self,
    ) -> None:
        proxy_config = self.root / "hagency-model-proxy.toml"
        proxy_config.write_text(
            'version = 1\ndefault_provider = "openai"\n[providers.openai]\nadapter = "openai"\n',
            encoding="utf-8",
        )
        state = mock.Mock(pid=123, host="127.0.0.1", port=9876)
        paths = mock.Mock(log=self.root / "service.log")
        with mock.patch.object(
            commands_service_module, "start_model_proxy", return_value=(state, paths)
        ) as start_mock:
            stdout, _stderr = self.run_cli(
                "service", "model-proxy", "start", "--port", "9876"
            )
        start_mock.assert_called_once_with(proxy_config, host="127.0.0.1", port=9876)
        self.assertIn("started model proxy: pid 123", stdout)

        state.host = "::1"
        with mock.patch.object(
            commands_service_module, "start_model_proxy", return_value=(state, paths)
        ):
            stdout, _stderr = self.run_cli(
                "service", "model-proxy", "start", "--host", "::1"
            )
        self.assertIn("http://[::1]:9876", stdout)

        _stdout, stderr = self.run_cli("serve", "start", expected=2)
        self.assertIn("No such command", stderr)
        _stdout, stderr = self.run_cli(
            "service",
            "model-proxy",
            "restart",
            "--host",
            "0.0.0.0",
            expected=2,
        )
        self.assertIn("loopback IP address", stderr)

    def test_serve_model_proxy_config_and_root_are_mutually_exclusive(self) -> None:
        _stdout, stderr = self.run_cli(
            "service",
            "model-proxy",
            "stop",
            "--root",
            str(self.root),
            "--config",
            "proxy.toml",
            expected=2,
        )
        self.assertIn("options are mutually exclusive", stderr)

    def test_serve_stop_and_restart_dispatch_lifecycle_operations(self) -> None:
        state = mock.Mock(pid=456, host="127.0.0.1", port=8765)
        paths = mock.Mock(log=self.root / "service.log")
        with mock.patch.object(
            commands_service_module, "stop_model_proxy", return_value=(True, paths)
        ) as stop_mock:
            stdout, _stderr = self.run_cli("service", "model-proxy", "stop")
        stop_mock.assert_called_once_with(self.root / "hagency-model-proxy.toml")
        self.assertIn("stopped model proxy", stdout)

        with mock.patch.object(
            commands_service_module, "restart_model_proxy", return_value=(state, paths)
        ) as restart_mock:
            stdout, _stderr = self.run_cli("service", "model-proxy", "restart")
        restart_mock.assert_called_once_with(
            self.root / "hagency-model-proxy.toml",
            host="127.0.0.1",
            port=8765,
        )
        self.assertIn("restarted model proxy: pid 456", stdout)

    def test_bash_completion_includes_commands_aliases_and_options(self) -> None:
        values, stderr = self.complete_bash("hgc ", 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            values,
            [
                "init",
                "source",
                "s",
                "skill",
                "profile",
                "p",
                "file",
                "service",
            ],
        )

        values, _stderr = self.complete_bash("hgc file ", 2)
        self.assertEqual(
            values,
            [
                "push",
                "pull",
                "sync",
                "init",
                "pack",
                "apply",
                "purge",
            ],
        )

        values, _stderr = self.complete_bash("hgc file sync --", 3)
        self.assertIn("--profile", values)
        self.assertIn("--dry-run", values)
        self.assertIn("--port", values)
        self.assertIn("--identity", values)
        self.assertIn("--exclude", values)
        self.assertIn("--skip-create", values)
        self.assertIn("--ignore-existing", values)
        self.assertNotIn("--force", values)
        self.assertNotIn("--git-changed", values)
        self.assertNotIn("--delete", values)
        self.assertNotIn("--update", values)

        values, _stderr = self.complete_bash("hgc file push --", 3)
        self.assertIn("--git-changed", values)
        self.assertIn("--delete", values)
        self.assertIn("--update", values)

        values, _stderr = self.complete_bash("hgc file push --", 3)
        self.assertIn("--git-changed", values)

        values, _stderr = self.complete_bash("hgc file pull --", 3)
        self.assertNotIn("--git-changed", values)
        self.assertIn("--delete", values)
        self.assertIn("--update", values)

        values, _stderr = self.complete_bash("hgc file init --", 3)
        self.assertIn("--root", values)
        self.assertIn("--force", values)
        self.assertIn("--dry-run", values)
        self.assertNotIn("--profile", values)

        values, _stderr = self.complete_bash("hgc file pack --", 3)
        for option in (
            "--root",
            "--profile",
            "--no-config",
            "--output",
            "--force",
            "--git-changed",
            "--exclude",
            "--dry-run",
        ):
            self.assertIn(option, values)
        self.assertNotIn("--delete", values)
        self.assertNotIn("--port", values)
        self.assertNotIn("--identity", values)

        values, _stderr = self.complete_bash("hgc file apply --", 3)
        for option in (
            "--root",
            "--delete",
            "--skip-create",
            "--ignore-existing",
            "--update",
            "--dry-run",
        ):
            self.assertIn(option, values)
        self.assertNotIn("--profile", values)
        self.assertNotIn("--exclude", values)
        self.assertNotIn("--git-changed", values)
        self.assertNotIn("--force", values)

        values, _stderr = self.complete_bash("hgc service model-proxy ", 3)
        self.assertEqual(values, ["start", "stop", "restart"])

        values, _stderr = self.complete_bash("hgc file ", 2)
        self.assertEqual(
            values, ["push", "pull", "sync", "init", "pack", "apply", "purge"]
        )

        values, _stderr = self.complete_bash("hgc file purge --", 3)
        self.assertIn("--dry-run", values)
        self.assertIn("--paths", values)

        values, _stderr = self.complete_bash("hgc source ", 2)
        self.assertEqual(values, ["list", "ls", "show", "add", "remove", "rm", "sync"])

        values, _stderr = self.complete_bash("hgc source sync --", 3)
        self.assertIn("--profile", values)
        self.assertIn("--reanchor", values)
        self.assertIn("--checkout-dir", values)

    def test_space_purge_help_and_argument_constraints(self) -> None:
        stdout, stderr = self.run_cli("file", "purge", "--help")
        self.assertEqual(stderr, "")
        self.assertIn("PATH", stdout)
        self.assertIn("--dry-run", stdout)
        self.assertIn("--paths", stdout)

        _stdout, stderr = self.run_cli("file", "purge", ".", "--paths", expected=2)
        self.assertIn("--paths cannot be combined", stderr)

        _stdout, stderr = self.run_cli(
            "file", "purge", "--dry-run", "--paths", expected=2
        )
        self.assertIn("--paths cannot be combined", stderr)

    def test_cli_import_does_not_eagerly_load_questionary(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import hagency_cli.cli; print('questionary' in sys.modules)",
            ],
            capture_output=True,
            check=True,
            env=environment,
            text=True,
        )

        self.assertEqual(result.stdout.strip(), "False")

    def test_space_purge_dispatches_normalized_paths_and_dry_run(self) -> None:
        report = mock.Mock(exit_code=0)
        ui = mock.sentinel.purge_ui
        with (
            mock.patch.object(
                commands_purge_ui_module, "QuestionaryPurgeUI", return_value=ui
            ),
            mock.patch.object(
                commands_file_module, "purge_space", return_value=report
            ) as purge_mock,
            mock.patch.object(
                commands_file_module, "render_purge_report"
            ) as render_mock,
        ):
            self.run_cli("file", "purge", ".", "nested/..", "--dry-run")

        request = purge_mock.call_args.args[0]
        self.assertEqual(request.paths, (self.root.resolve(), self.root.resolve()))
        self.assertTrue(request.dry_run)
        self.assertEqual(purge_mock.call_args.kwargs, {"ui": ui})
        render_mock.assert_called_once_with(report)

    def test_space_purge_preserves_symlink_path_for_core_validation(self) -> None:
        scan_link = self.root / "scan-link"
        try:
            scan_link.symlink_to(self.root / "local-source", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"could not create symlink: {exc}")

        with (
            mock.patch.object(commands_purge_ui_module, "QuestionaryPurgeUI"),
            mock.patch.object(
                commands_file_module, "purge_space", return_value=mock.Mock(exit_code=0)
            ) as purge_mock,
            mock.patch.object(commands_file_module, "render_purge_report"),
        ):
            self.run_cli("file", "purge", "scan-link")

        request = purge_mock.call_args.args[0]
        self.assertEqual(request.paths, (scan_link.absolute(),))

    def test_space_purge_questionary_prompts_preserve_safe_defaults(self) -> None:
        choice = mock.Mock(label="project | target | 1 KiB", id="choice-id")
        choice.preselected = True
        choice.project_path = self.root / "project"
        checkbox_prompt = mock.Mock()
        checkbox_prompt.unsafe_ask.return_value = ["choice-id"]
        with mock.patch.object(
            commands_purge_ui_module.questionary,
            "checkbox",
            return_value=checkbox_prompt,
        ) as checkbox_mock:
            selected = commands_purge_ui_module.QuestionaryPurgeUI().select((choice,))

        self.assertEqual(selected, ("choice-id",))
        checkbox_choices = checkbox_mock.call_args.kwargs["choices"]
        self.assertIsInstance(
            checkbox_choices[0], commands_purge_ui_module.questionary.Separator
        )
        self.assertEqual(checkbox_choices[0].title, str(choice.project_path))
        checkbox_choice = checkbox_choices[1]
        self.assertEqual(checkbox_choice.title, choice.label)
        self.assertEqual(checkbox_choice.value, choice.id)
        self.assertTrue(checkbox_choice.checked)
        checkbox_prompt.unsafe_ask.assert_called_once_with()

        confirm_prompt = mock.Mock()
        confirm_prompt.unsafe_ask.return_value = True
        exact_path = self.root / "project" / "target"
        output = io.StringIO()
        with (
            mock.patch.object(
                commands_purge_ui_module.questionary,
                "confirm",
                return_value=confirm_prompt,
            ) as confirm_mock,
            contextlib.redirect_stdout(output),
        ):
            confirmed = commands_purge_ui_module.QuestionaryPurgeUI().confirm_exact(
                (exact_path,), 1024
            )

        self.assertTrue(confirmed)
        self.assertIn(str(exact_path), output.getvalue())
        self.assertEqual(confirm_mock.call_args.kwargs["default"], False)
        confirm_prompt.unsafe_ask.assert_called_once_with()

    def test_space_purge_rendering_shows_exact_plan_results_and_path_edits(
        self,
    ) -> None:
        selected_path = self.root / "old-project" / "node_modules"
        recent_path = self.root / "active-project" / "target"
        choices = (
            files_purge_models_module.PurgeChoice(
                id="old",
                exact_path=selected_path,
                project_path=selected_path.parent,
                artifact_kind="node_modules",
                size_bytes=1024,
                activity=files_purge_models_module.Activity.OLD,
                preselected=True,
            ),
            files_purge_models_module.PurgeChoice(
                id="recent",
                exact_path=recent_path,
                project_path=recent_path.parent,
                artifact_kind="target",
                size_bytes=None,
                activity=files_purge_models_module.Activity.RECENT,
                preselected=False,
            ),
        )
        preview = files_purge_models_module.PurgeReport(
            disposition=files_purge_models_module.PurgeDisposition.PREVIEW,
            roots=(self.root,),
            choices=choices,
            selected_paths=(selected_path,),
            results=(
                files_purge_models_module.PurgeItemResult(
                    exact_path=selected_path,
                    disposition=files_purge_models_module.ItemDisposition.WOULD_REMOVE,
                    size_bytes=1024,
                ),
            ),
            issues=(
                files_purge_models_module.PurgeIssue(
                    "scan_notice",
                    self.root,
                    "one directory was unreadable",
                    is_failure=False,
                ),
            ),
            known_bytes=1024,
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            commands_purge_render_module.render_purge_report(preview)

        rendered = stdout.getvalue()
        self.assertIn(
            f"[selected] old | node_modules | 1.0 KiB | {selected_path}", rendered
        )
        self.assertIn(f"[ ] recent | target | size unknown | {recent_path}", rendered)
        self.assertIn(f"Would remove: {selected_path}", rendered)
        self.assertIn("Preview complete: 1 artifact(s), known size 1.0 KiB.", rendered)
        self.assertIn("Warning [scan_notice]", stderr.getvalue())

        edit_report = files_purge_models_module.PathsEditReport(
            config_path=self.root / "space-purge-paths",
            before_roots=(self.root / "before",),
            after_roots=(self.root / "after",),
            editor="vi",
            issues=(),
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            commands_purge_render_module.render_paths_edit_report(edit_report)
        rendered = stdout.getvalue()
        self.assertIn("Before:\n", rendered)
        self.assertIn(str(self.root / "before"), rendered)
        self.assertIn("After:\n", rendered)
        self.assertIn(str(self.root / "after"), rendered)

    def test_space_purge_paths_dispatch_and_exit_mapping(self) -> None:
        paths_report = mock.Mock(exit_code=0)
        with (
            mock.patch.object(
                commands_file_module, "edit_purge_paths", return_value=paths_report
            ) as edit_mock,
            mock.patch.object(
                commands_file_module, "render_paths_edit_report"
            ) as render_mock,
        ):
            self.run_cli("file", "purge", "--paths")
        edit_mock.assert_called_once_with()
        render_mock.assert_called_once_with(paths_report)

        failed_paths_report = mock.Mock(exit_code=5)
        with (
            mock.patch.object(
                commands_file_module,
                "edit_purge_paths",
                return_value=failed_paths_report,
            ),
            mock.patch.object(commands_file_module, "render_paths_edit_report"),
        ):
            self.run_cli("file", "purge", "--paths", expected=5)

        purge_report = mock.Mock(exit_code=7)
        with (
            mock.patch.object(commands_purge_ui_module, "QuestionaryPurgeUI"),
            mock.patch.object(
                commands_file_module, "purge_space", return_value=purge_report
            ),
            mock.patch.object(commands_file_module, "render_purge_report"),
        ):
            self.run_cli("file", "purge", expected=7)

        with (
            mock.patch.object(commands_purge_ui_module, "QuestionaryPurgeUI"),
            mock.patch.object(
                commands_file_module, "purge_space", side_effect=KeyboardInterrupt
            ),
        ):
            self.run_cli("file", "purge", expected=130)

    def test_bash_completion_includes_workspace_catalog_values(self) -> None:
        values, _stderr = self.complete_bash("hgc source show ", 3)
        self.assertEqual(values, ["local-source"])

        values, _stderr = self.complete_bash("hgc profile show ", 3)
        self.assertEqual(values, ["content"])

        values, _stderr = self.complete_bash("hgc skill add ", 3)
        self.assertIn("local-one", values)
        self.assertIn("external-one", values)
        self.assertIn("workspace:skills/local-one", values)
        self.assertIn("local-source:nested/external-one", values)

        self.write_skill(self.root / "skills" / "external-one")
        values, _stderr = self.complete_bash("hgc skill add ", 3)
        self.assertNotIn("external-one", values)
        self.assertIn("workspace:skills/external-one", values)
        self.assertIn("local-source:nested/external-one", values)

    def test_bash_completion_filters_selectors_and_used_values(self) -> None:
        values, _stderr = self.complete_bash("hgc source sync local-source ", 4)
        self.assertNotIn("local-source", values)

        values, _stderr = self.complete_bash(
            "hgc profile add research -AS local-source -i ", 7
        )
        self.assertIn("*", values)
        self.assertIn("nested/external-one", values)

        values, _stderr = self.complete_bash(
            "hgc profile add research -AS local-source -i nested/external-one -i ",
            9,
        )
        self.assertNotIn("nested/external-one", values)

        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested/external-one"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        values, _stderr = self.complete_bash("hgc profile update content -RS ", 5)
        self.assertIn("local-source", values)
        self.assertIn("external-one", values)
        self.assertIn("local-source:nested/external-one", values)
        self.assertNotIn("workspace", values)

    def test_bash_completion_respects_root_checkout_and_directory_context(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        words = f"hgc source show --root {self.root} "
        values, _stderr = self.complete_bash(words, 5, cwd=outside)
        self.assertEqual(values, ["local-source"])

        checkout_override = self.root / "other-checkouts"
        self.write_skill(checkout_override / "remote-source" / "skills" / "remote-one")
        self.append_remote_source()
        words = f"hgc skill add --root {self.root} --checkout-dir {checkout_override} remote"
        values, _stderr = self.complete_bash(words, 7, cwd=outside)
        self.assertIn("remote-one", values)
        self.assertIn("remote-source:skills/remote-one", values)

        directory = outside / "consumer"
        directory.mkdir()
        values, _stderr = self.complete_bash("hgc init --root con", 3, cwd=outside)
        self.assertEqual(values, ["consumer/"])

    def test_bash_directory_completion_lists_children_for_empty_value(self) -> None:
        values, stderr = self.complete_bash("hgc init --root ", 3)

        self.assertEqual(stderr, "")
        self.assertEqual(values, ["local-source/", "profiles/", "skills/"])

    def test_bash_completion_catalog_failures_are_silent(self) -> None:
        missing = self.root / "missing"
        missing.mkdir()
        words = f"hgc source show --root {missing} "
        values, stderr = self.complete_bash(words, 5)
        self.assertEqual(values, [])
        self.assertEqual(stderr, "")

        broken = self.root / "broken"
        broken.mkdir()
        (broken / "hagency-config.toml").write_text("[source\n", encoding="utf-8")
        words = f"hgc source show --root {broken} "
        values, stderr = self.complete_bash(words, 5)
        self.assertEqual(values, [])
        self.assertEqual(stderr, "")

        malformed = self.root / "malformed"
        malformed.mkdir()
        (malformed / "hagency-config.toml").write_text(
            'source = "not-a-table"\n', encoding="utf-8"
        )
        words = f"hgc source show --root {malformed} "
        values, stderr = self.complete_bash(words, 5)
        self.assertEqual(values, [])
        self.assertEqual(stderr, "")

        self.append_remote_source()
        values, stderr = self.complete_bash("hgc skill add remote-source:", 3)
        self.assertEqual(values, [])
        self.assertEqual(stderr, "")

    def test_completion_script_and_isolated_install_bind_only_hgc(self) -> None:
        stdout, stderr = self.run_main("--show-completion", "bash")
        self.assertEqual(stderr, "")
        self.assertIn("_HGC_COMPLETE=complete_bash", stdout)
        self.assertIn("complete -o default -F _hgc_completion hgc", stdout)
        self.assertNotIn(" hagency", stdout)

        home = self.root / "isolated-home"
        home.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            stdout, stderr = self.run_main("--install-completion", "bash")
        self.assertEqual(stderr, "")
        self.assertIn("bash completion installed", stdout)
        completion = home / ".bash_completions" / "hgc.sh"
        self.assertTrue(completion.is_file())
        self.assertIn(
            "complete -o default -F _hgc_completion hgc",
            completion.read_text(encoding="utf-8"),
        )
        self.assertFalse((home / ".bash_completions" / "hagency.sh").exists())

    def test_show_completion_rejects_an_unknown_explicit_shell(self) -> None:
        stdout, stderr = self.run_main("--show-completion", "junk", expected=2)

        self.assertEqual(stdout, "")
        self.assertIn("Invalid value for '--show-completion'", stderr)
        self.assertIn("junk", stderr)

    def test_init_creates_config_and_refuses_existing_without_force(self) -> None:
        new_root = self.root / "new-workspace"
        stdout, _stderr = self.run_cli("init", "--root", str(new_root), cwd=self.root)
        self.assertIn("initialized hagency workspace:", stdout)
        self.assertTrue((new_root / "hagency-config.toml").exists())
        with (new_root / "hagency-config.toml").open("rb") as handle:
            defaults = tomllib.load(handle)["defaults"]
        self.assertEqual(defaults["checkout_dir"], "~/Projects/references")
        self.assertEqual(defaults["depth"], 1)
        self.assertNotIn("checkout_dir_windows", defaults)

        _stdout, stderr = self.run_cli("init", "--root", str(new_root), expected=1)
        self.assertIn("workspace config already exists", stderr)

        stdout, _stderr = self.run_cli(
            "init", "--root", str(new_root), "--force", "--dry-run"
        )
        self.assertIn("Would overwrite workspace config:", stdout)

    def test_workspace_discovery_and_root_override(self) -> None:
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)

        stdout, _stderr = self.run_cli("source", "list", cwd=nested)
        self.assertIn("local-source\tlocal", stdout)

        other = self.root / "other"
        other.mkdir()
        (other / "hagency-config.toml").write_text(
            textwrap.dedent(
                """
                [source.other-source]
                path = "other-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        stdout, _stderr = self.run_cli(
            "source", "list", "--root", str(other), cwd=nested
        )
        self.assertIn("other-source\tlocal", stdout)
        self.assertNotIn("local-source\tlocal", stdout)

    def test_workspace_resolution_precedence_includes_installed_source(self) -> None:
        current = self.root / "current"
        nested = current / "nested"
        nested.mkdir(parents=True)
        (current / "hagency-config.toml").write_text("invalid = [\n", encoding="utf-8")
        explicit = self.root / "explicit"
        explicit.mkdir()
        (explicit / "hagency-config.toml").write_text("", encoding="utf-8")
        installed_module = (
            self.root
            / "tools"
            / "hagency-cli"
            / "src"
            / "hagency_cli"
            / "workspace"
            / "discovery.py"
        )

        with mock.patch.object(
            workspace_discovery_module, "__file__", str(installed_module)
        ):
            self.assertEqual(
                workspace_discovery_module.resolve_workspace_root(
                    str(explicit), nested
                ),
                explicit.resolve(),
            )
            self.assertEqual(
                workspace_discovery_module.resolve_workspace_root(None, nested),
                current.resolve(),
            )

    def test_workspace_resolution_falls_back_to_installed_source(self) -> None:
        installed_module = (
            self.root
            / "tools"
            / "hagency-cli"
            / "src"
            / "hagency_cli"
            / "workspace"
            / "discovery.py"
        )
        with tempfile.TemporaryDirectory() as outside_value:
            outside = Path(outside_value)
            with mock.patch.object(
                workspace_discovery_module, "__file__", str(installed_module)
            ):
                stdout, _stderr = self.run_cli("source", "list", cwd=outside)
                values, completion_stderr = self.complete_bash(
                    "hgc source show ", 3, cwd=outside
                )

        self.assertIn("local-source\tlocal", stdout)
        self.assertEqual(values, ["local-source"])
        self.assertEqual(completion_stderr, "")

    def test_workspace_resolution_without_any_marker_preserves_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as outside_value,
            tempfile.TemporaryDirectory() as installed_value,
        ):
            outside = Path(outside_value)
            installed_module = (
                Path(installed_value)
                / "site-packages"
                / "hagency_cli"
                / "workspace"
                / "discovery.py"
            )
            with mock.patch.object(
                workspace_discovery_module, "__file__", str(installed_module)
            ):
                _stdout, stderr = self.run_cli(
                    "source", "list", cwd=outside, expected=1
                )

        self.assertEqual(
            stderr, f"Error: not a hagency workspace: {outside.resolve()}\n"
        )

    def test_workspace_resolution_ignores_unrelated_install_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as outside_value:
            outside = Path(outside_value)
            for module_path in (
                "site-packages/hagency_cli/workspace.py",
                "checkout/tools/hagency-cli/src/hagency_cli/workspace.py",
            ):
                with (
                    self.subTest(module_path=module_path),
                    mock.patch.object(
                        workspace_discovery_module,
                        "__file__",
                        str(self.root / module_path),
                    ),
                ):
                    _stdout, stderr = self.run_cli(
                        "source", "list", cwd=outside, expected=1
                    )
                    self.assertIn("not a hagency workspace", stderr)

    def test_workspace_source_fallback_requires_a_config_file(self) -> None:
        checkout = self.root / "checkout"
        (checkout / "hagency-config.toml").mkdir(parents=True)
        installed_module = (
            checkout
            / "tools"
            / "hagency-cli"
            / "src"
            / "hagency_cli"
            / "workspace"
            / "discovery.py"
        )
        with (
            tempfile.TemporaryDirectory() as outside_value,
            mock.patch.object(
                workspace_discovery_module, "__file__", str(installed_module)
            ),
        ):
            _stdout, stderr = self.run_cli(
                "source", "list", cwd=Path(outside_value), expected=1
            )
        self.assertIn("not a hagency workspace", stderr)

    def test_init_does_not_fall_back_to_installed_source(self) -> None:
        installed_module = (
            self.root
            / "tools"
            / "hagency-cli"
            / "src"
            / "hagency_cli"
            / "workspace"
            / "discovery.py"
        )
        original_config = self.config_path.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as outside_value:
            outside = Path(outside_value)
            with mock.patch.object(
                workspace_discovery_module, "__file__", str(installed_module)
            ):
                stdout, _stderr = self.run_cli("init", cwd=outside)

            self.assertTrue((outside / "hagency-config.toml").is_file())
            self.assertIn(f"initialized hagency workspace: {outside}", stdout)

        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original_config)

    def test_windows_git_bash_path_normalization(self) -> None:
        with mock.patch.object(paths_module.os, "name", "nt"):
            self.assertEqual(
                paths_module.normalize_windows_shell_path("/c/Users/me/project"),
                "C:/Users/me/project",
            )
            self.assertEqual(paths_module.normalize_windows_shell_path("/d"), "D:/")
            self.assertEqual(
                paths_module.normalize_windows_shell_path("/d/Projects/references"),
                "D:/Projects/references",
            )
            self.assertEqual(
                paths_module.normalize_windows_shell_path(r"C:\Users\me\project"),
                r"C:\Users\me\project",
            )

        self.assertEqual(
            paths_module.normalize_windows_shell_path("/c/Users/me/project"),
            "/c/Users/me/project",
        )

    def test_checkout_directory_selection_uses_platform_specific_default(self) -> None:
        defaults = {
            "checkout_dir": "~/Projects/references",
            "checkout_dir_windows": "/d/Projects/references",
        }

        self.assertEqual(
            workspace_sources_module.configured_checkout_dir(
                defaults, checkout_override=None, windows=True
            ),
            "/d/Projects/references",
        )
        self.assertEqual(
            workspace_sources_module.configured_checkout_dir(
                defaults, checkout_override=None, windows=False
            ),
            "~/Projects/references",
        )

    def test_checkout_directory_selection_falls_back_when_windows_default_is_missing_or_empty(
        self,
    ) -> None:
        cases = (
            {"checkout_dir": "~/Projects/references"},
            {"checkout_dir": "~/Projects/references", "checkout_dir_windows": ""},
        )

        for defaults in cases:
            with self.subTest(defaults=defaults):
                self.assertEqual(
                    workspace_sources_module.configured_checkout_dir(
                        defaults, checkout_override=None, windows=True
                    ),
                    "~/Projects/references",
                )

    def test_checkout_dir_cli_override_wins_over_configured_defaults(self) -> None:
        defaults = {
            "checkout_dir": "~/Projects/references",
            "checkout_dir_windows": "/d/Projects/references",
        }
        self.assertEqual(
            workspace_sources_module.configured_checkout_dir(
                defaults,
                checkout_override="cli-checkouts",
                windows=True,
            ),
            "cli-checkouts",
        )

        self.append_remote_source()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ncheckout_dir_windows = "/d/Projects/references"',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source",
            "show",
            "remote-source",
            "--checkout-dir",
            "cli-checkouts",
        )

        self.assertIn(
            f"resolved_path: {self.root / 'cli-checkouts' / 'remote-source'}", stdout
        )

    def test_remote_source_without_checkout_directory_uses_platform_neutral_error(
        self,
    ) -> None:
        self.config_path.write_text(
            textwrap.dedent(
                """
                [source.remote-source.remote]
                url = "https://example.invalid/acme/ExamplePack.git"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("source", "show", "remote-source", expected=1)

        self.assertIn("remote plus a configured checkout directory", stderr)
        self.assertNotIn("defaults.checkout_dir", stderr)

    def test_dry_run_command_output_is_shell_neutral(self) -> None:
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            result = workspace_git_module.run(
                ["git", "status"], cwd=self.root, dry_run=True, progress=render_event
            )

        output = stdout.getvalue()
        self.assertIsNone(result)
        self.assertIn(f"+ cwd: {self.root}", output)
        self.assertIn("+ cmd: git status", output)
        self.assertNotIn("&&", output)

    def test_source_add_list_show_remove_use_generic_schema(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "example-pack",
            "--url",
            "https://example.invalid/acme/ExamplePack.git",
            "--ref",
            "main",
        )
        self.assertIn("added source: example-pack", stdout)
        added = self.read_config()["source"]["example-pack"]
        self.assertEqual(
            added["remote"]["url"], "https://example.invalid/acme/ExamplePack.git"
        )
        self.assertNotIn("skills_path", added)

        stdout, _stderr = self.run_cli("source", "list")
        self.assertIn("name\ttype\tpath\turl\tref", stdout)
        self.assertIn("example-pack\tremote", stdout)
        stdout, _stderr = self.run_cli("source", "ls")
        self.assertIn("example-pack\tremote", stdout)

        stdout, _stderr = self.run_cli("source", "show", "example-pack")
        self.assertIn("name: example-pack", stdout)
        self.assertIn("remote.ref: main", stdout)
        self.assertNotIn("skills_path", stdout)

        stdout, _stderr = self.run_cli("source", "rm", "example-pack")
        self.assertIn("removed source: example-pack", stdout)
        self.assertNotIn("example-pack", self.read_config()["source"])

    def test_source_add_rewrite_preserves_windows_checkout_directory(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ncheckout_dir_windows = "/d/Projects/references"',
            ),
            encoding="utf-8",
        )

        self.run_cli(
            "source",
            "add",
            "example-pack",
            "--url",
            "https://example.invalid/acme/ExamplePack.git",
        )

        self.assertEqual(
            self.read_config()["defaults"]["checkout_dir_windows"],
            "/d/Projects/references",
        )

    def test_source_top_level_short_alias(self) -> None:
        stdout, _stderr = self.run_cli("s", "ls")

        self.assertIn("local-source\tlocal", stdout)

    def test_source_add_url_positional_infers_name(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
        )

        self.assertIn("added source: ExamplePack", stdout)
        added = self.read_config()["source"]["ExamplePack"]
        self.assertEqual(
            added["remote"]["url"], "https://example.invalid/acme/ExamplePack.git"
        )

    def test_source_add_url_positional_strips_trailing_slash_and_git_suffix(
        self,
    ) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git/",
        )

        self.assertIn("added source: ExamplePack", stdout)
        self.assertIn("ExamplePack", self.read_config()["source"])

    def test_source_add_scp_style_url_positional_infers_name(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "git@example.invalid:acme/ExamplePack.git",
        )

        self.assertIn("added source: ExamplePack", stdout)
        added = self.read_config()["source"]["ExamplePack"]
        self.assertEqual(
            added["remote"]["url"], "git@example.invalid:acme/ExamplePack.git"
        )

    def test_source_add_url_positional_name_override(self) -> None:
        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
            "--name",
            "example-pack-alt",
        )

        self.assertIn("added source: example-pack-alt", stdout)
        self.assertIn("example-pack-alt", self.read_config()["source"])

    def test_source_add_sync_dry_run_prints_added_source_sync(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/acme/ExamplePack.git",
            "--sync",
            "--dry-run",
        )

        self.assertIn("Would add source:", stdout)
        self.assertIn("[source.ExamplePack.remote]", stdout)
        self.assertIn("sync source [1/1] ExamplePack", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 1 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )
        self.assertNotIn("ExamplePack", self.read_config().get("source", {}))

    def test_source_add_sync_writes_config_and_syncs_added_source(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            workspace_operations_sources_module, "sync_source"
        ) as sync_mock:
            stdout, _stderr = self.run_cli(
                "source",
                "add",
                "https://example.invalid/acme/ExamplePack.git",
                "--sync",
            )

        self.assertIn("added source: ExamplePack", stdout)
        self.assertIn("sync source [1/1] ExamplePack", stdout)
        self.assertIn("ExamplePack", self.read_config()["source"])
        sync_mock.assert_called_once()
        source_arg = sync_mock.call_args.args[0]
        self.assertEqual(source_arg.name, "ExamplePack")
        self.assertEqual(sync_mock.call_args.kwargs["dry_run"], False)
        self.assertEqual(sync_mock.call_args.kwargs["depth"], 1)

    def test_source_add_url_positional_keeps_basename_when_no_conflict(self) -> None:
        stdout, _stderr = self.run_cli(
            "source", "add", "https://example.invalid/anthropic/skills.git"
        )

        self.assertIn("added source: skills", stdout)
        self.assertIn("skills", self.read_config()["source"])

    def test_source_add_inferred_name_conflict_uses_owner_prefixed_name(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        stdout, _stderr = self.run_cli(
            "source", "add", "https://example.invalid/mattpocock/skills.git"
        )

        self.assertIn("added source: mattpocock/skills", stdout)
        added = self.read_config()["source"]["mattpocock/skills"]
        self.assertEqual(
            added["remote"]["url"], "https://example.invalid/mattpocock/skills.git"
        )

    def test_source_add_scp_style_inferred_name_conflict_uses_owner_prefixed_name(
        self,
    ) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        stdout, _stderr = self.run_cli(
            "source", "add", "git@example.invalid:mattpocock/skills.git"
        )

        self.assertIn("added source: mattpocock/skills", stdout)
        added = self.read_config()["source"]["mattpocock/skills"]
        self.assertEqual(
            added["remote"]["url"], "git@example.invalid:mattpocock/skills.git"
        )

    def test_source_add_inferred_owner_prefixed_conflict_fails_with_custom_name_hint(
        self,
    ) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")
        self.run_cli("source", "add", "https://example.invalid/mattpocock/skills.git")

        _stdout, stderr = self.run_cli(
            "source", "add", "https://example.invalid/mattpocock/skills", expected=1
        )

        self.assertIn("source already exists: skills", stderr)
        self.assertIn("owner-prefixed source also exists: mattpocock/skills", stderr)
        self.assertIn("pass --name <custom-name>", stderr)

    def test_source_add_explicit_name_conflict_does_not_fallback(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/anthropic/skills.git")

        _stdout, stderr = self.run_cli(
            "source",
            "add",
            "https://example.invalid/mattpocock/skills.git",
            "--name",
            "skills",
            expected=1,
        )

        self.assertIn("source already exists: skills", stderr)
        self.assertNotIn("mattpocock/skills", self.read_config()["source"])

    def test_source_add_inferred_name_conflict_fails_when_owner_name_cannot_be_inferred(
        self,
    ) -> None:
        self.run_cli("source", "add", "https://example.invalid/skills.git")

        _stdout, stderr = self.run_cli(
            "source", "add", "https://example.invalid/skills", expected=1
        )

        self.assertIn("source already exists: skills", stderr)
        self.assertIn("could not infer owner/repo name from URL", stderr)
        self.assertIn("pass --name <custom-name>", stderr)

    def test_source_add_inferred_names_remain_case_sensitive(self) -> None:
        self.run_cli("source", "add", "https://example.invalid/acme/ExamplePack.git")

        stdout, _stderr = self.run_cli(
            "source", "add", "https://example.invalid/acme/example-pack"
        )
        self.assertIn("added source: example-pack", stdout)

    def test_source_sync_default_depth_config_applies_to_clone_dry_run(self) -> None:
        self.append_remote_source()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--dry-run")

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertNotIn("&&", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 1 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )

    def test_source_sync_depth_flag_overrides_default_depth_config(self) -> None:
        self.append_remote_source()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 1',
            ),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source", "sync", "remote-source", "--depth", "2", "--dry-run"
        )

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertIn(
            "git clone --origin origin --branch main --depth 2 https://example.invalid/acme/ExamplePack.git",
            stdout,
        )

    def test_source_sync_depth_rejects_non_positive_values(self) -> None:
        _stdout, stderr = self.run_cli(
            "source", "sync", "--depth", "0", "--dry-run", expected=2
        )
        self.assertIn("Invalid value for '--depth'", stderr)
        self.assertIn("x>=1", stderr)

    def test_source_sync_default_depth_config_rejects_non_positive_values(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8").replace(
                'checkout_dir = "checkouts"',
                'checkout_dir = "checkouts"\ndepth = 0',
            ),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("source", "sync", "--dry-run", expected=1)

        self.assertIn("defaults.depth must be a positive integer", stderr)

    def test_source_sync_existing_shallow_checkout_fast_forwards_after_remote_advances(
        self,
    ) -> None:
        origin, checkout = self.create_remote_source_checkout()
        latest = self.commit_file(origin, "two", "two")

        stdout, _stderr = self.run_cli("source", "sync", "remote-source")

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), latest)
        self.assertNotIn("reset", stdout)

    def test_source_sync_recovers_depth_one_remote_tracking_divergence(self) -> None:
        origin, checkout = self.create_remote_source_checkout()
        latest = self.commit_file(origin, "two", "two")
        self.run_git(checkout, "fetch", "--depth", "1", "origin")

        self.assertIn(
            "[ahead 1, behind 1]",
            self.run_git(checkout, "status", "--short", "--branch"),
        )

        stdout, _stderr = self.run_cli("source", "sync", "remote-source")

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), latest)
        self.assertNotIn("reset", stdout)

    def test_source_sync_real_divergence_fails_without_resetting_local_commit(
        self,
    ) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        local = self.commit_file(checkout, "local", "local")
        self.commit_file(origin, "remote", "remote")

        stdout, stderr = self.run_cli("source", "sync", "remote-source", expected=1)

        self.assertIn("sync source [1/1] remote-source", stdout)
        self.assertIn("cannot fast-forward", stderr)
        self.assertIn(
            "Tip: if these checkouts are disposable and local-only commits may be discarded, run:\n"
            "  hgc source sync remote-source --reanchor",
            stderr,
        )
        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), local)
        self.assertNotIn("reset", stdout)

    def test_source_sync_reanchor_replaces_clean_divergent_branch_without_persistent_state(
        self,
    ) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        local = self.commit_file(checkout, "local", "local")
        remote = self.commit_file(origin, "remote", "remote")

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--reanchor")

        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), remote)
        self.assertEqual(self.run_git(checkout, "status", "--porcelain"), "")
        self.assertIn(
            f"reanchor source remote-source: main {local} -> origin/main {remote}",
            stdout,
        )
        self.assertEqual(
            self.run_git(
                checkout, "for-each-ref", "--format=%(refname)", "refs/hagency"
            ),
            "",
        )
        self.assertFalse((checkout / ".git" / "hagency").exists())

    def test_source_sync_reanchor_rejects_staged_unstaged_and_untracked_changes(
        self,
    ) -> None:
        for dirty_kind in ("staged", "unstaged", "untracked"):
            with self.subTest(dirty_kind=dirty_kind):
                name = f"{dirty_kind}-source"
                origin, checkout = self.create_remote_source_pair(name, shallow=False)
                local = self.commit_file(checkout, "local", "local")
                self.commit_file(origin, "remote", "remote")
                self.write_remote_source_config(name, origin, depth=None)

                if dirty_kind == "staged":
                    (checkout / "file.txt").write_text("staged", encoding="utf-8")
                    self.run_git(checkout, "add", "file.txt")
                elif dirty_kind == "unstaged":
                    (checkout / "file.txt").write_text("unstaged", encoding="utf-8")
                else:
                    (checkout / "untracked.txt").write_text(
                        "untracked", encoding="utf-8"
                    )

                _stdout, stderr = self.run_cli(
                    "source", "sync", name, "--reanchor", expected=1
                )

                self.assertIn(
                    "cannot reanchor main: checkout has staged, tracked, or untracked changes",
                    stderr,
                )
                self.assertNotIn("Tip:", stderr)
                self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), local)
                self.assertNotEqual(self.run_git(checkout, "status", "--porcelain"), "")

    def test_source_sync_reanchor_refuses_to_overwrite_ignored_file(self) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        local = self.commit_file(checkout, "local", "local")
        (origin / "generated.txt").write_text("remote", encoding="utf-8")
        self.run_git(origin, "add", "generated.txt")
        self.run_git(
            origin,
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-m",
            "remote",
        )
        (checkout / ".git" / "info" / "exclude").write_text(
            "generated.txt\n", encoding="utf-8"
        )
        (checkout / "generated.txt").write_text("keep", encoding="utf-8")
        self.assertEqual(self.run_git(checkout, "status", "--porcelain"), "")

        _stdout, stderr = self.run_cli(
            "source", "sync", "remote-source", "--reanchor", expected=1
        )

        self.assertIn("git checkout --no-overwrite-ignore -B main origin/main", stderr)
        self.assertNotIn("Tip:", stderr)
        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), local)
        self.assertEqual(
            (checkout / "generated.txt").read_text(encoding="utf-8"), "keep"
        )

    def test_source_sync_reanchor_handles_unrelated_upstream_history(self) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        old_head = self.run_git(checkout, "rev-parse", "HEAD")
        self.run_git(origin, "checkout", "--orphan", "rewritten")
        self.run_git(origin, "rm", "-rf", ".")
        rewritten = self.commit_file(origin, "rewritten", "rewritten")
        self.run_git(origin, "branch", "-M", "main")

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--reanchor")

        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), rewritten)
        self.assertIn(
            f"reanchor source remote-source: main {old_head} -> origin/main {rewritten}",
            stdout,
        )

    def test_source_sync_reanchor_batch_updates_eligible_sources_and_reports_dirty_source(
        self,
    ) -> None:
        fast_origin, fast_checkout = self.create_remote_source_pair(
            "fast-source", shallow=False
        )
        fast_remote = self.commit_file(fast_origin, "fast remote", "fast remote")

        rewrite_origin, rewrite_checkout = self.create_remote_source_pair(
            "rewrite-source", shallow=False
        )
        self.commit_file(rewrite_checkout, "rewrite local", "rewrite local")
        rewrite_remote = self.commit_file(
            rewrite_origin, "rewrite remote", "rewrite remote"
        )

        dirty_origin, dirty_checkout = self.create_remote_source_pair(
            "dirty-source", shallow=False
        )
        dirty_local = self.commit_file(dirty_checkout, "dirty local", "dirty local")
        self.commit_file(dirty_origin, "dirty remote", "dirty remote")
        (dirty_checkout / "untracked.txt").write_text("keep", encoding="utf-8")

        self.write_remote_sources_config(
            {
                "fast-source": fast_origin,
                "rewrite-source": rewrite_origin,
                "dirty-source": dirty_origin,
            },
            depth=None,
        )

        stdout, stderr = self.run_cli("source", "sync", "--reanchor", expected=1)

        self.assertEqual(self.run_git(fast_checkout, "rev-parse", "HEAD"), fast_remote)
        self.assertEqual(
            self.run_git(rewrite_checkout, "rev-parse", "HEAD"), rewrite_remote
        )
        self.assertEqual(self.run_git(dirty_checkout, "rev-parse", "HEAD"), dirty_local)
        self.assertIn("reanchor source rewrite-source:", stdout)
        self.assertIn("source dirty-source failed: cannot reanchor main", stderr)
        self.assertIn("source sync failed for: dirty-source", stderr)
        self.assertNotIn("Tip:", stderr)

    def test_source_sync_aggregates_reanchor_tip_for_all_divergent_sources(
        self,
    ) -> None:
        first_origin, first_checkout = self.create_remote_source_pair(
            "first-source", shallow=False
        )
        first_local = self.commit_file(first_checkout, "first local", "first local")
        self.commit_file(first_origin, "first remote", "first remote")

        second_origin, second_checkout = self.create_remote_source_pair(
            "second-source", shallow=False
        )
        second_local = self.commit_file(second_checkout, "second local", "second local")
        self.commit_file(second_origin, "second remote", "second remote")

        self.write_remote_sources_config(
            {
                "first-source": first_origin,
                "second-source": second_origin,
            },
            depth=None,
        )

        _stdout, stderr = self.run_cli("source", "sync", expected=1)

        self.assertEqual(stderr.count("Tip:"), 1)
        self.assertIn(
            "  hgc source sync first-source second-source --reanchor",
            stderr,
        )
        self.assertIn("source sync failed for: first-source, second-source", stderr)
        self.assertEqual(self.run_git(first_checkout, "rev-parse", "HEAD"), first_local)
        self.assertEqual(
            self.run_git(second_checkout, "rev-parse", "HEAD"), second_local
        )

    def test_source_sync_reanchor_dry_run_only_describes_conditional_behavior(
        self,
    ) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        old_head = self.commit_file(checkout, "local", "local")
        self.commit_file(origin, "remote", "remote")
        old_remote = self.run_git(checkout, "rev-parse", "origin/main")

        stdout, _stderr = self.run_cli(
            "source", "sync", "remote-source", "--reanchor", "--dry-run"
        )

        self.assertIn("git fetch origin +main:refs/remotes/origin/main", stdout)
        self.assertIn(
            "Would reanchor source remote-source to origin/main "
            "only if the fetched history cannot fast-forward and the checkout is clean",
            stdout,
        )
        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), old_head)
        self.assertEqual(self.run_git(checkout, "rev-parse", "origin/main"), old_remote)

    def test_source_sync_reanchor_supports_profile_and_slice_selection(self) -> None:
        origins: dict[str, Path] = {}
        for name in ("first-source", "second-source", "third-source"):
            origin, _checkout = self.create_remote_source_pair(name, shallow=False)
            origins[name] = origin
        self.write_remote_sources_config(origins, depth=None)
        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.first-source]

                [skill.second-source]

                [skill.third-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source",
            "sync",
            "--profile",
            "content",
            "-s",
            "2:",
            "--reanchor",
            "--dry-run",
        )

        self.assertNotIn("sync source [1/3] first-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("Would reanchor source second-source to origin/main", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)
        self.assertIn("Would reanchor source third-source to origin/main", stdout)

    def test_source_sync_reanchor_keeps_fast_forward_and_clone_paths(self) -> None:
        origin, checkout = self.create_remote_source_checkout(shallow=False)
        latest = self.commit_file(origin, "remote", "remote")

        stdout, _stderr = self.run_cli("source", "sync", "remote-source", "--reanchor")

        self.assertEqual(self.run_git(checkout, "rev-parse", "HEAD"), latest)
        self.assertNotIn("reanchor source", stdout)

        clone_checkout = self.root / "checkouts" / "clone-source"
        self.write_remote_source_config("clone-source", origin, depth=None)

        stdout, _stderr = self.run_cli("source", "sync", "clone-source", "--reanchor")

        self.assertEqual(self.run_git(clone_checkout, "rev-parse", "HEAD"), latest)
        self.assertNotIn("reanchor source", stdout)

    def test_source_slice_parsing_valid_values(self) -> None:
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice("4:", 5), [4, 5]
        )
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice("2:4", 5), [2, 3, 4]
        )
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice(":3", 5), [1, 2, 3]
        )
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice("4", 5), [4]
        )
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice("1,3", 5), [1, 3]
        )
        self.assertEqual(
            workspace_operations_sources_module.parse_source_slice("1,3:", 5),
            [1, 3, 4, 5],
        )

    def test_source_slice_parsing_invalid_values(self) -> None:
        for value in ["0", "-1", "4:2", "abc", "1:2:3", "6", "1,,3", ",1", "1,"]:
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(WorkspaceError),
            ):
                workspace_operations_sources_module.parse_source_slice(value, 5)

    def test_source_sync_slice_dry_run_uses_original_indexes(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "2:3")

        self.assertNotIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_slice_accepts_jumping_indexes(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "1,3")

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertNotIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_slice_accepts_jumping_index_plus_tail(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        stdout, _stderr = self.run_cli("source", "sync", "--dry-run", "-s", "1,3:")

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertNotIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_profile_slice_applies_after_profile_selection(self) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")
        (self.root / "profiles" / "content" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]

                [skill.second-source]

                [skill.third-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "source", "sync", "--profile", "content", "-s", "2:", "--dry-run"
        )

        self.assertNotIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)

    def test_source_sync_failure_continues_and_summarizes_without_traceback(
        self,
    ) -> None:
        self.append_local_source("second-source")
        self.append_local_source("third-source")

        def fake_sync(
            source,
            *,
            dry_run: bool,
            depth: int | None = None,
            reanchor: bool = False,
            progress=None,
        ) -> None:
            if source.name == "second-source":
                raise subprocess.CalledProcessError(128, ["git", "fetch", "origin"])

        with mock.patch.object(
            workspace_operations_sources_module, "sync_source", side_effect=fake_sync
        ):
            stdout, stderr = self.run_cli("source", "sync", expected=1)

        self.assertIn("sync source [1/3] local-source", stdout)
        self.assertIn("sync source [2/3] second-source", stdout)
        self.assertIn("sync source [3/3] third-source", stdout)
        self.assertIn("source second-source failed", stderr)
        self.assertIn("source sync failed for: second-source", stderr)
        self.assertNotIn("Tip:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_git_fetch_uses_hardcoded_retries(self) -> None:
        source_path = self.root / "checkout"
        source_path.mkdir()
        source = workspace_sources_module.Source(
            name="remote-source",
            path=source_path,
            remote=workspace_sources_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        current_remote = subprocess.CompletedProcess(
            ["git"], 0, "https://example.invalid/acme/ExamplePack.git\n", ""
        )
        calls: list[list[str]] = []

        def fail_run(cmd, *, cwd=None, dry_run: bool = False, progress=None):
            calls.append(cmd)
            raise subprocess.CalledProcessError(128, cmd)

        stdout = io.StringIO()
        with (
            mock.patch.object(workspace_sources_module, "git_ok", return_value=True),
            mock.patch.object(
                workspace_sources_module.subprocess, "run", return_value=current_remote
            ),
            mock.patch.object(workspace_sources_module, "run", side_effect=fail_run),
            mock.patch.object(workspace_sources_module.time, "sleep"),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            workspace_sources_module.sync_source(
                source, dry_run=False, depth=1, progress=render_event
            )

        fetch_calls = [cmd for cmd in calls if cmd[:2] == ["git", "fetch"]]
        self.assertEqual(len(fetch_calls), 4)
        self.assertIn("retry 1/3 after git fetch failed", stdout.getvalue())
        self.assertIn("retry 3/3 after git fetch failed", stdout.getvalue())

    def test_git_clone_uses_hardcoded_retries(self) -> None:
        source = workspace_sources_module.Source(
            name="remote-source",
            path=self.root / "checkout",
            remote=workspace_sources_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        calls: list[list[str]] = []

        def fail_run(cmd, *, cwd=None, dry_run: bool = False, progress=None):
            calls.append(cmd)
            raise subprocess.CalledProcessError(128, cmd)

        stdout = io.StringIO()
        with (
            mock.patch.object(workspace_sources_module, "run", side_effect=fail_run),
            mock.patch.object(workspace_sources_module.time, "sleep"),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            workspace_sources_module.sync_source(
                source, dry_run=False, depth=1, progress=render_event
            )

        clone_calls = [cmd for cmd in calls if cmd[:2] == ["git", "clone"]]
        self.assertEqual(len(clone_calls), 4)
        self.assertIn("retry 1/3 after git clone failed", stdout.getvalue())

    def test_sync_source_existing_checkout_uses_depth_on_fetch(self) -> None:
        source_path = self.root / "checkout"
        source_path.mkdir()
        source = workspace_sources_module.Source(
            name="remote-source",
            path=source_path,
            remote=workspace_sources_module.Remote(
                name="origin",
                url="https://example.invalid/acme/ExamplePack.git",
                ref="main",
            ),
        )
        missing_remote = subprocess.CompletedProcess(["git"], 1, "", "")

        stdout = io.StringIO()
        with (
            mock.patch.object(workspace_sources_module, "git_ok", return_value=True),
            mock.patch.object(
                workspace_sources_module.subprocess, "run", return_value=missing_remote
            ),
            contextlib.redirect_stdout(stdout),
        ):
            workspace_sources_module.sync_source(
                source, dry_run=True, depth=1, progress=render_event
            )

        self.assertIn("git fetch --depth 1 origin", stdout.getvalue())

    def test_profile_list_formats_profiles_sorted_by_name(self) -> None:
        (self.root / "profiles" / "alpha").mkdir()
        (self.root / "profiles" / "alpha" / "config.toml").write_text(
            textwrap.dedent(
                """
                name = "alpha"
                description = "Alpha profile."

                [skill.workspace]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("profile", "list")
        lines = stdout.strip().splitlines()
        self.assertEqual(lines[0], "name\tdescription\tskills")
        self.assertEqual(lines[1], "alpha\tAlpha profile.\tworkspace")
        self.assertEqual(lines[2], "content\t-\t-")
        stdout, _stderr = self.run_cli("profile", "ls")
        self.assertIn("alpha\tAlpha profile.\tworkspace", stdout)

    def test_profile_top_level_short_alias(self) -> None:
        stdout, _stderr = self.run_cli("p", "ls")

        self.assertIn("content\t-\t-", stdout)

    def test_profile_add_metadata_only_and_duplicate_rejected(self) -> None:
        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "research",
            "--description",
            "Research profile.",
        )

        self.assertIn("added profile: research", stdout)
        profile = self.read_profile("research")
        self.assertEqual(profile["name"], "research")
        self.assertEqual(profile["description"], "Research profile.")
        self.assertNotIn("skill", profile)

        _stdout, stderr = self.run_cli("profile", "add", "research", expected=1)
        self.assertIn("profile already exists: research", stderr)

    def test_profile_add_with_initial_skill_and_dry_run(self) -> None:
        self.write_skill(self.root / "local-source" / "external-two")

        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "example-pack",
            "-AS",
            "local-source",
            "-i",
            "nested",
            "-e",
            "external-two",
            "--dry-run",
        )
        self.assertIn("Would create profile:", stdout)
        self.assertIn("[skill.local-source]", stdout)
        self.assertIn('include = ["nested"]', stdout)
        self.assertFalse((self.root / "profiles" / "example-pack").exists())

        stdout, _stderr = self.run_cli(
            "profile", "add", "example-pack", "-AS", "local-source"
        )
        self.assertIn("added profile: example-pack", stdout)
        self.assertEqual(self.read_profile("example-pack")["skill"]["local-source"], {})

    def test_profile_add_accepts_legacy_values_after_inline_include(self) -> None:
        self.run_main(
            "profile",
            "add",
            "research",
            "-AS",
            "local-source",
            "--include=nested",
            "external-one",
        )

        self.assertEqual(
            self.read_profile("research")["skill"]["local-source"]["include"],
            ["nested", "external-one"],
        )

    def test_profile_add_skill_name_infers_source_and_include(self) -> None:
        stdout, _stderr = self.run_cli(
            "profile",
            "add",
            "example-pack",
            "-AS",
            "external-one",
            "--dry-run",
        )

        self.assertIn("[skill.local-source]", stdout)
        self.assertIn('include = ["external-one"]', stdout)

    def test_profile_add_skill_name_conflict_fails(self) -> None:
        self.write_skill(self.root / "other-source" / "nested" / "external-one")
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """

                    [source.other-source]
                    path = "other-source"
                    """
                )
            )

        _stdout, stderr = self.run_cli(
            "profile", "add", "example-pack", "-AS", "external-one", expected=1
        )

        self.assertIn("skill name 'external-one' is ambiguous. Choose one:", stderr)
        self.assertIn(
            "hgc profile add example-pack -AS local-source:nested/external-one", stderr
        )
        self.assertIn(
            "hgc profile add example-pack -AS other-source:nested/external-one", stderr
        )

    def test_profile_update_add_skill_merges_and_dedupes(self) -> None:
        self.write_skill(self.root / "local-source" / "other")
        self.write_skill(self.root / "local-source" / "old")
        self.write_skill(self.root / "local-source" / "draft")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                exclude = ["old"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "-i",
            "nested",
            "other",
            "-e",
            "old",
            "draft",
        )

        self.assertIn("updated profile: content", stdout)
        skill = self.read_profile()["skill"]["local-source"]
        self.assertEqual(skill["include"], ["nested", "other"])
        self.assertEqual(skill["exclude"], ["old", "draft"])

    def test_profile_update_repeated_include_exclude_options(self) -> None:
        self.write_skill(self.root / "local-source" / "draft")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text('name = "content"\n', encoding="utf-8")

        self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "-i",
            "nested",
            "-i",
            "draft",
            "-e",
            "draft",
            "-e",
            "nested",
        )

        skill = self.read_profile()["skill"]["local-source"]
        self.assertEqual(skill["include"], ["nested", "draft"])
        self.assertEqual(skill["exclude"], ["draft", "nested"])

    def test_profile_update_add_skill_name_infers_source_and_include(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text('name = "content"\n', encoding="utf-8")

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-AS", "external-one"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["include"], ["external-one"]
        )

    def test_profile_update_add_source_selector_reference(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text('name = "content"\n', encoding="utf-8")

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-AS", "local-source:nested/external-one"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["include"],
            ["nested/external-one"],
        )

    def test_profile_update_remove_source_selector_reference(self) -> None:
        self.write_skill(self.root / "local-source" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested/external-one", "external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-RS", "local-source:nested/external-one"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["include"], ["external-two"]
        )

    def test_profile_update_ambiguous_skill_name_suggests_source_selector_references(
        self,
    ) -> None:
        self.write_skill(self.root / "local-source" / "skills" / "write")
        self.write_skill(
            self.root / "local-source" / "plugins" / "waza" / "skills" / "write"
        )
        profile_path = self.root / "profiles" / "content" / "config.toml"
        before = profile_path.read_text(encoding="utf-8")

        _stdout, stderr = self.run_cli(
            "profile", "update", "content", "-AS", "write", expected=1
        )

        self.assertIn("skill name 'write' is ambiguous. Choose one:", stderr)
        self.assertIn(
            "hgc profile update content -AS local-source:plugins/waza/skills/write",
            stderr,
        )
        self.assertIn(
            "hgc profile update content -AS local-source:skills/write", stderr
        )
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

    def test_profile_update_include_ambiguous_selector_fails_before_write(self) -> None:
        self.write_skill(self.root / "local-source" / "skills" / "write")
        self.write_skill(
            self.root / "local-source" / "plugins" / "waza" / "skills" / "write"
        )
        profile_path = self.root / "profiles" / "content" / "config.toml"
        before = profile_path.read_text(encoding="utf-8")

        _stdout, stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "--include",
            "write",
            expected=1,
        )

        self.assertIn(
            "skill selector 'write' for source local-source matched multiple candidates",
            stderr,
        )
        self.assertIn("plugins/waza/skills/write", stderr)
        self.assertIn("skills/write", stderr)
        self.assertEqual(profile_path.read_text(encoding="utf-8"), before)

    def test_profile_update_short_alias_with_profile_short_alias(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["old"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "p", "u", "content", "-AS", "local-source", "-i", "nested"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["include"], ["old", "nested"]
        )

    def test_profile_update_existing_all_include_stays_all(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        self.run_cli(
            "profile", "update", "content", "-AS", "local-source", "--include", "nested"
        )
        self.assertNotIn("include", self.read_profile()["skill"]["local-source"])

        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["*"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        self.run_cli(
            "profile", "update", "content", "-AS", "local-source", "--include", "nested"
        )
        self.assertEqual(self.read_profile()["skill"]["local-source"]["include"], ["*"])

    def test_profile_update_add_skill_replace(self) -> None:
        self.write_skill(self.root / "local-source" / "other")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested", "old"]
                exclude = ["draft"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile",
            "update",
            "content",
            "-AS",
            "local-source",
            "--include",
            "other",
            "--replace",
        )

        self.assertIn("updated profile: content", stdout)
        skill = self.read_profile()["skill"]["local-source"]
        self.assertEqual(skill, {"include": ["other"]})

    def test_profile_update_remove_skill_with_short_alias(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-RS", "local-source"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertNotIn("skill", self.read_profile())

    def test_profile_update_remove_skill_name_from_include_list(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["external-one", "external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-RS", "external-one"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["include"], ["external-two"]
        )

    def test_profile_update_remove_skill_name_from_full_source_adds_exclude(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "profile", "update", "content", "-RS", "external-one"
        )

        self.assertIn("updated profile: content", stdout)
        self.assertEqual(
            self.read_profile()["skill"]["local-source"]["exclude"], ["external-one"]
        )

    def test_profile_update_unknown_source_rejected(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile", "update", "content", "-AS", "missing", expected=1
        )
        self.assertIn("unknown source or skill: missing", stderr)

    def test_profile_update_unknown_skill_mentions_unsynced_remote_sources(
        self,
    ) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                textwrap.dedent(
                    """

                    [source.remote-pack.remote]
                    url = "https://example.invalid/acme/RemotePack.git"
                    """
                )
            )

        _stdout, stderr = self.run_cli(
            "profile", "update", "content", "-AS", "frontend-design", expected=1
        )

        self.assertIn("unknown source or skill: frontend-design", stderr)
        self.assertIn("hgc source sync remote-pack", stderr)

    def test_profile_update_include_requires_add_skill(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile", "update", "content", "--include", "nested", expected=1
        )
        self.assertIn("--include and --exclude require --add-skill", stderr)

    def test_profile_rejects_unsafe_names(self) -> None:
        _stdout, stderr = self.run_cli("profile", "add", "../bad", expected=1)
        self.assertIn("unsafe profile name", stderr)

    def test_profile_remove_deletes_directory_and_dry_run_does_not(self) -> None:
        (self.root / "profiles" / "scratch" / "notes").mkdir(parents=True)
        (self.root / "profiles" / "scratch" / "config.toml").write_text(
            'name = "scratch"\n', encoding="utf-8"
        )
        (self.root / "profiles" / "scratch" / "notes" / "README.md").write_text(
            "keep", encoding="utf-8"
        )

        stdout, _stderr = self.run_cli("profile", "remove", "scratch", "--dry-run")
        self.assertIn("Would remove profile directory:", stdout)
        self.assertTrue((self.root / "profiles" / "scratch").exists())

        stdout, _stderr = self.run_cli("profile", "rm", "scratch")
        self.assertIn("removed profile: scratch", stdout)
        self.assertFalse((self.root / "profiles" / "scratch").exists())

    def test_profile_init_dir_discovers_skills_under_workspace_layout(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "apply", "-d", str(target), "content")
        self.assertIn("local-one", stdout)
        self.assertIn("external-one", stdout)
        self.assertTrue((target / ".agents" / "skills" / "local-one").is_symlink())
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())

    def test_profile_init_path_is_exact_absolute_skills_directory(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        skills_dir = self.root / "custom" / "skills"

        self.run_cli("profile", "apply", "--path", str(skills_dir), "content")

        destination = skills_dir / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )
        self.assertFalse((skills_dir / ".agents").exists())

    def test_profile_init_relative_path_uses_invocation_cwd_with_explicit_root(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        self.run_cli(
            "profile",
            "apply",
            "--path",
            "shared/skills",
            "content",
            "--root",
            str(self.root),
            cwd=invocation_cwd,
        )

        destination = invocation_cwd / "shared" / "skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / "shared").exists())

    def test_profile_init_relative_dir_uses_invocation_cwd_with_explicit_root(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        self.run_cli(
            "profile",
            "apply",
            "--dir",
            "project",
            "content",
            "--root",
            str(self.root),
            cwd=invocation_cwd,
        )

        destination = invocation_cwd / "project" / ".agents" / "skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / "project").exists())

    def test_profile_init_requires_a_destination(self) -> None:
        _stdout, stderr = self.run_cli("profile", "apply", "content", expected=2)

        self.assertIn("one of the options is required", stderr)
        self.assertIn("--path", stderr)
        self.assertIn("--dir", stderr)

    def test_profile_init_rejects_path_and_dir_together(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile",
            "apply",
            "--path",
            str(self.root / "skills"),
            "--dir",
            str(self.root / "project"),
            "content",
            expected=2,
        )

        self.assertIn("options are mutually exclusive", stderr)

    def test_install_commands_reject_non_directory_skills_destination(self) -> None:
        destination = self.root / "not-a-directory"
        destination.write_text("keep\n", encoding="utf-8")
        commands = (
            ("profile", "apply", "--path", str(destination), "content"),
            ("skill", "add", "external-one", "--path", str(destination)),
        )

        for command in commands:
            with self.subTest(command=command):
                _stdout, stderr = self.run_cli(*command, expected=1)
                self.assertIn(
                    f"skills destination is not a directory: {destination}", stderr
                )

        self.assertEqual(destination.read_text(encoding="utf-8"), "keep\n")

    def test_skill_add_dry_run_rejects_non_directory_destination_ancestor(self) -> None:
        blocker = self.root / "blocker"
        blocker.write_text("keep\n", encoding="utf-8")

        _stdout, stderr = self.run_cli(
            "skill",
            "add",
            "external-one",
            "--path",
            str(blocker / "skills"),
            "--dry-run",
            expected=1,
        )

        self.assertIn(f"skills destination is not a directory: {blocker}", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(blocker.read_text(encoding="utf-8"), "keep\n")

    def test_install_commands_reject_broken_skills_destination_symlink(self) -> None:
        destination = self.root / "broken-skills"
        destination.symlink_to(self.root / "missing-skills", target_is_directory=True)
        commands = (
            ("profile", "apply", "--path", str(destination), "content"),
            ("skill", "add", "external-one", "--path", str(destination)),
        )

        for command in commands:
            with self.subTest(command=command):
                _stdout, stderr = self.run_cli(*command, expected=1)
                self.assertIn(
                    f"skills destination is a broken symlink: {destination}", stderr
                )

        self.assertTrue(destination.is_symlink())
        self.assertFalse(destination.exists())

    def test_profile_init_copy_mode_creates_independent_skill_directory(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        skills_dir = self.root / "target" / "skills"

        stdout, _stderr = self.run_cli(
            "profile", "apply", "-p", str(skills_dir), "content", "-cp"
        )

        copied = skills_dir / "external-one"
        source = self.root / "local-source" / "nested" / "external-one"
        self.assertIn("copy", stdout)
        self.assertTrue(copied.is_dir())
        self.assertFalse(copied.is_symlink())
        self.assertTrue((copied / "SKILL.md").exists())

        (copied / "local-note.md").write_text("target-only\n", encoding="utf-8")
        self.assertFalse((source / "local-note.md").exists())

    def test_profile_init_symlink_dry_run_uses_shell_neutral_output(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli(
            "profile", "apply", "-d", str(target), "content", "--dry-run"
        )

        self.assertIn("link", stdout)
        self.assertNotIn("ln -s", stdout)
        self.assertFalse((target / ".agents" / "skills" / "external-one").exists())

    def test_profile_init_link_mode_copy_dry_run_does_not_create_destination(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli(
            "profile",
            "apply",
            "-d",
            str(target),
            "content",
            "--link-mode",
            "copy",
            "--dry-run",
        )

        self.assertIn("copy", stdout)
        self.assertNotIn("ln -s", stdout)
        self.assertFalse((target / ".agents" / "skills" / "external-one").exists())

    def test_profile_init_windows_default_uses_junction(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        skills_dir = self.root / "target" / "skills"

        with (
            mock.patch.object(
                workspace_skills_module, "is_windows_platform", return_value=True
            ),
            mock.patch.object(
                workspace_skills_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["powershell"], 0),
            ) as run,
        ):
            stdout, _stderr = self.run_cli(
                "profile", "apply", "-p", str(skills_dir), "content"
            )

        self.assertIn("junction", stdout)
        run.assert_called_once()
        command = run.call_args[0][0]
        self.assertEqual(command[0], "powershell")
        self.assertEqual(command[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(len(command), 5)
        self.assertIn("New-Item -ItemType Junction", command[4])
        self.assertIn("$env:HAGENCY_PROFILE_JUNCTION_LINK", command[4])
        self.assertIn("$env:HAGENCY_PROFILE_JUNCTION_TARGET", command[4])
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(
            Path(child_env["HAGENCY_PROFILE_JUNCTION_LINK"]),
            skills_dir / "external-one",
        )
        self.assertEqual(
            Path(child_env["HAGENCY_PROFILE_JUNCTION_TARGET"]),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_windows_junction_paths_are_not_parsed_as_powershell_code(self) -> None:
        link = Path(r"C:\Users\测试 User\work & tools\link's [x]")
        target = Path(r"D:\Source Trees\pack;name\$literal's")

        with (
            mock.patch.dict(
                workspace_skills_module.os.environ,
                {"HAGENCY_TEST_SENTINEL": "kept"},
                clear=True,
            ),
            mock.patch.object(
                workspace_skills_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["powershell"], 0),
            ) as run,
        ):
            workspace_skills_module.create_windows_junction(link, target)

            self.assertNotIn(
                "HAGENCY_PROFILE_JUNCTION_LINK", workspace_skills_module.os.environ
            )
            self.assertNotIn(
                "HAGENCY_PROFILE_JUNCTION_TARGET", workspace_skills_module.os.environ
            )

        run.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(
            command[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
        )
        self.assertEqual(len(command), 5)
        self.assertNotIn(str(link), command[4])
        self.assertNotIn(str(target), command[4])
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["HAGENCY_TEST_SENTINEL"], "kept")
        self.assertEqual(child_env["HAGENCY_PROFILE_JUNCTION_LINK"], str(link))
        self.assertEqual(child_env["HAGENCY_PROFILE_JUNCTION_TARGET"], str(target))
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertTrue(run.call_args.kwargs["text"])

    def test_profile_init_windows_junction_dry_run_does_not_call_powershell(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        with (
            mock.patch.object(
                workspace_skills_module, "is_windows_platform", return_value=True
            ),
            mock.patch.object(workspace_skills_module.subprocess, "run") as run,
        ):
            stdout, _stderr = self.run_cli(
                "profile", "apply", "-d", str(target), "content", "--dry-run"
            )

        self.assertIn("junction", stdout)
        run.assert_not_called()
        self.assertFalse((target / ".agents" / "skills" / "external-one").exists())

    def test_profile_init_explicit_junction_is_windows_only(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with mock.patch.object(
            workspace_skills_module, "is_windows_platform", return_value=False
        ):
            _stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                "--link-mode",
                "junction",
                expected=1,
            )

        self.assertIn("skill link mode junction is only supported on Windows", stderr)

    def test_profile_init_copy_refuses_existing_destination(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"
        self.run_cli("profile", "apply", "-d", str(target), "content", "-cp")
        copied = target / ".agents" / "skills" / "external-one"
        marker = copied / "local-note.md"
        marker.write_text("keep\n", encoding="utf-8")

        _stdout, stderr = self.run_cli(
            "profile", "apply", "-d", str(target), "content", "-cp", expected=1
        )

        self.assertIn("refusing to overwrite existing skill destination", stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_profile_init_copy_conflicts_with_explicit_link_modes(self) -> None:
        for link_mode in ("symlink", "junction"):
            with self.subTest(link_mode=link_mode):
                _stdout, stderr = self.run_cli(
                    "profile",
                    "apply",
                    "-d",
                    str(self.root / "target"),
                    "content",
                    "-cp",
                    "--link-mode",
                    link_mode,
                    expected=1,
                )

            self.assertIn(
                f"-cp cannot be combined with --link-mode {link_mode}", stderr
            )

    def test_profile_init_copy_long_option_is_not_registered(self) -> None:
        _stdout, stderr = self.run_cli(
            "profile",
            "apply",
            "-d",
            str(self.root / "target"),
            "content",
            "--copy",
            expected=2,
        )

        self.assertIn("No such option: --copy", stderr)
        self.assertIn("-cp", stderr)

    def test_profile_init_windows_symlink_error_mentions_administrator_mode(
        self,
    ) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with (
            mock.patch.object(
                workspace_skills_module, "is_windows_platform", return_value=True
            ),
            mock.patch.object(
                workspace_skills_module.os,
                "symlink",
                side_effect=OSError("permission denied"),
            ),
        ):
            _stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                "--link-mode",
                "symlink",
                expected=1,
            )

        self.assertIn("could not create symlink", stderr)
        self.assertIn("PowerShell or Git Bash as Administrator", stderr)
        self.assertIn("--link-mode junction", stderr)
        self.assertIn("-cp", stderr)

    def test_duplicate_discovered_skill_names_prompt_for_source(self) -> None:
        selected = self.root / "local-source" / "other" / "external-one"
        self.write_skill(selected)
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        prompt = mock.Mock()
        prompt.unsafe_ask.return_value = selected
        with (
            mock.patch.object(
                commands_skill_ui_module.QuestionarySkillConflictUI,
                "is_interactive",
                return_value=True,
            ),
            mock.patch.object(
                commands_skill_ui_module.questionary, "select", return_value=prompt
            ) as select,
        ):
            stdout, _stderr = self.run_cli(
                "profile", "apply", "-d", str(self.root / "target"), "content"
            )

        installed = self.root / "target" / ".agents" / "skills" / "external-one"
        self.assertIn("external-one", stdout)
        self.assertEqual(installed.resolve(), selected.resolve())
        select.assert_called_once()
        choices = select.call_args.kwargs["choices"]
        self.assertEqual(
            [choice.value for choice in choices],
            [
                self.root / "local-source" / "nested" / "external-one",
                selected,
            ],
        )
        prompt.unsafe_ask.assert_called_once_with()

    def test_duplicate_discovered_skill_names_fail_without_terminal(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with mock.patch.object(
            commands_skill_ui_module.QuestionarySkillConflictUI,
            "is_interactive",
            return_value=False,
        ):
            _stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                expected=1,
            )

        self.assertIn("duplicate discovered skill name", stderr)
        self.assertIn("rerun in an interactive terminal", stderr)

    def test_duplicate_discovered_skill_names_dry_run_previews_without_terminal(
        self,
    ) -> None:
        other = self.root / "local-source" / "other" / "external-one"
        self.write_skill(other)
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with mock.patch.object(
            commands_skill_ui_module.QuestionarySkillConflictUI,
            "is_interactive",
            return_value=False,
        ):
            stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                "--dry-run",
            )

        self.assertEqual(stderr, "")
        self.assertIn("conflict 'external-one'", stdout)
        self.assertIn(
            str(self.root / "local-source" / "nested" / "external-one"), stdout
        )
        self.assertIn(str(other), stdout)
        self.assertFalse((self.root / "target").exists())

    def test_duplicate_discovered_skill_names_across_sources_show_source_labels(
        self,
    ) -> None:
        first = self.root / "local-source" / "nested" / "external-one"
        second = self.root / "second-source" / "external-one"
        self.write_skill(second)
        self.config_path.write_text(
            textwrap.dedent(
                """
                [defaults]
                checkout_dir = "checkouts"

                [source.local-source]
                path = "local-source"

                [source.second-source]
                path = "second-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]

                [skill.second-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        prompt = mock.Mock()
        prompt.unsafe_ask.return_value = second
        with (
            mock.patch.object(
                commands_skill_ui_module.QuestionarySkillConflictUI,
                "is_interactive",
                return_value=True,
            ),
            mock.patch.object(
                commands_skill_ui_module.questionary, "select", return_value=prompt
            ) as select,
        ):
            self.run_cli("profile", "apply", "-d", str(self.root / "target"), "content")

        choices = select.call_args.kwargs["choices"]
        self.assertEqual(
            [choice.title for choice in choices],
            [f"local-source: {first}", f"second-source: {second}"],
        )
        installed = self.root / "target" / ".agents" / "skills" / "external-one"
        self.assertEqual(installed.resolve(), second.resolve())

    def test_duplicate_discovered_skill_prompt_terminal_error_is_clean(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        prompt = mock.Mock()
        prompt.unsafe_ask.side_effect = OSError("terminal unavailable")
        with (
            mock.patch.object(
                commands_skill_ui_module.QuestionarySkillConflictUI,
                "is_interactive",
                return_value=True,
            ),
            mock.patch.object(
                commands_skill_ui_module.questionary, "select", return_value=prompt
            ),
        ):
            _stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                expected=1,
            )

        self.assertIn("interactive skill source selection failed", stderr)
        self.assertIn("terminal unavailable", stderr)
        self.assertFalse((self.root / "target").exists())

    def test_duplicate_discovered_skill_selection_can_be_cancelled(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        prompt = mock.Mock()
        prompt.unsafe_ask.return_value = None
        with (
            mock.patch.object(
                commands_skill_ui_module.QuestionarySkillConflictUI,
                "is_interactive",
                return_value=True,
            ),
            mock.patch.object(
                commands_skill_ui_module.questionary, "select", return_value=prompt
            ),
        ):
            _stdout, stderr = self.run_cli(
                "profile",
                "apply",
                "-d",
                str(self.root / "target"),
                "content",
                expected=1,
            )

        self.assertIn("skill source selection cancelled", stderr)
        self.assertFalse((self.root / "target").exists())

    def test_duplicate_selection_of_same_skill_path_is_deduplicated(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["*", "nested/external-one"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with mock.patch.object(
            commands_skill_ui_module.QuestionarySkillConflictUI, "select"
        ) as select:
            self.run_cli("profile", "apply", "-d", str(self.root / "target"), "content")

        installed = self.root / "target" / ".agents" / "skills" / "external-one"
        self.assertEqual(
            installed.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )
        select.assert_not_called()

    def test_skill_include_accepts_prefix_path(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "apply", "-d", str(target), "content")
        self.assertIn("external-one", stdout)
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())

    def test_skill_include_star_and_exclude(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                include = ["*"]
                exclude = ["external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        target = self.root / "target"

        stdout, _stderr = self.run_cli("profile", "apply", "-d", str(target), "content")
        self.assertIn("external-one", stdout)
        self.assertNotIn("external-two", stdout)
        self.assertTrue((target / ".agents" / "skills" / "external-one").is_symlink())
        self.assertFalse((target / ".agents" / "skills" / "external-two").exists())

    def test_skill_add_installs_unique_name_under_invocation_cwd(self) -> None:
        invocation_cwd = self.root / "projects" / "example"
        invocation_cwd.mkdir(parents=True)

        stdout, stderr = self.run_cli(
            "skill", "add", "external-one", cwd=invocation_cwd
        )

        destination = invocation_cwd / ".agents" / "skills" / "external-one"
        self.assertEqual(stderr, "")
        self.assertIn(f"link {destination} ->", stdout)
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_skill_add_root_only_affects_discovery_and_default_destination(
        self,
    ) -> None:
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        self.run_cli(
            "skill",
            "add",
            "local-source:nested/external-one",
            "--root",
            str(self.root),
            cwd=invocation_cwd,
        )

        destination = invocation_cwd / ".agents" / "skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_skill_add_path_is_exact_absolute_skills_directory(self) -> None:
        skills_dir = self.root / "custom" / "skills"

        self.run_cli("skill", "add", "external-one", "-p", str(skills_dir))

        destination = skills_dir / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )
        self.assertFalse((skills_dir / ".agents").exists())

    def test_skill_add_relative_path_uses_invocation_cwd(self) -> None:
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        self.run_cli(
            "skill",
            "add",
            "external-one",
            "--path",
            "shared/skills",
            cwd=invocation_cwd,
        )

        destination = invocation_cwd / "shared" / "skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertFalse((self.root / "shared").exists())

    def test_skill_add_path_expands_user_home(self) -> None:
        home = self.root / "user-home"

        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}):
            self.run_cli("skill", "add", "external-one", "--path", "~/shared-skills")

        destination = home / "shared-skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_skill_add_dir_installs_under_workspace_layout(self) -> None:
        target_workspace = self.root / "consumer-project"

        self.run_cli("skill", "add", "external-one", "-d", str(target_workspace))

        destination = target_workspace / ".agents" / "skills" / "external-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(),
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_skill_add_destination_options_are_mutually_exclusive(self) -> None:
        conflicts = (
            ("--path", str(self.root / "skills"), "--dir", str(self.root / "project")),
            ("--path", str(self.root / "skills"), "--global"),
            ("--dir", str(self.root / "project"), "--global"),
        )

        for options in conflicts:
            with self.subTest(options=options):
                _stdout, stderr = self.run_cli(
                    "skill", "add", "external-one", *options, expected=2
                )
                self.assertIn("options are mutually exclusive", stderr)

    def test_skill_add_global_installs_under_user_home(self) -> None:
        home = self.root / "user-home"
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        with mock.patch.object(Path, "home", return_value=home):
            self.run_cli("skill", "add", "local-one", "--global", cwd=invocation_cwd)

        destination = home / ".agents" / "skills" / "local-one"
        self.assertTrue(destination.is_symlink())
        self.assertEqual(
            destination.resolve(), (self.root / "skills" / "local-one").resolve()
        )
        self.assertFalse((invocation_cwd / ".agents").exists())

    def test_skill_add_dry_run_does_not_create_destination(self) -> None:
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        stdout, _stderr = self.run_cli(
            "skill",
            "add",
            "external-one",
            "--path",
            "planned/skills",
            "--dry-run",
            cwd=invocation_cwd,
        )

        destination = invocation_cwd / "planned" / "skills" / "external-one"
        self.assertIn(f"link {destination} ->", stdout)
        self.assertFalse((invocation_cwd / "planned").exists())

    def test_skill_add_rejects_source_only_wildcard_and_ambiguous_references(
        self,
    ) -> None:
        stdout, stderr = self.run_cli("skill", "add", "local-source")
        self.assertEqual(stderr, "")
        self.assertIn("external-one", stdout)

        self.write_skill(self.root / "local-source" / "other" / "external-two")
        _stdout, stderr = self.run_cli("skill", "add", "local-source:*", expected=1)
        self.assertIn(
            "skill reference 'local-source:*' matched 2 skills; choose one exact SOURCE:selector",
            stderr,
        )

        self.write_skill(self.root / "skills" / "external-one")
        _stdout, stderr = self.run_cli("skill", "add", "external-one", expected=1)
        self.assertIn("skill name 'external-one' is ambiguous. Choose one:", stderr)
        self.assertIn("hgc skill add workspace:skills/external-one", stderr)
        self.assertIn("hgc skill add local-source:nested/external-one", stderr)

    def test_skill_add_reports_unknown_and_unsynced_references(self) -> None:
        _stdout, stderr = self.run_cli("skill", "add", "missing", expected=1)
        self.assertIn("unknown source or skill: missing", stderr)

        self.append_remote_source("remote-source")
        stdout, stderr = self.run_cli(
            "skill", "add", "remote-source:missing", "--dry-run"
        )
        self.assertIn("not yet verified", stdout)
        self.assertEqual(stderr, "")

    def test_skill_add_is_idempotent_retargets_links_and_refuses_real_destinations(
        self,
    ) -> None:
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        self.run_cli(
            "skill", "add", "local-source:nested/external-one", cwd=invocation_cwd
        )
        stdout, _stderr = self.run_cli(
            "skill",
            "add",
            "local-source:nested/external-one",
            cwd=invocation_cwd,
        )
        destination = invocation_cwd / ".agents" / "skills" / "external-one"
        self.assertIn(f"ok {destination} ->", stdout)

        workspace_skill = self.root / "skills" / "external-one"
        self.write_skill(workspace_skill)
        stdout, _stderr = self.run_cli(
            "skill",
            "add",
            "workspace:skills/external-one",
            cwd=invocation_cwd,
        )
        self.assertIn(f"remove {destination}", stdout)
        self.assertEqual(destination.resolve(), workspace_skill.resolve())

        destination.unlink()
        destination.mkdir()
        (destination / "keep.txt").write_text("keep", encoding="utf-8")
        _stdout, stderr = self.run_cli(
            "skill",
            "add",
            "local-source:nested/external-one",
            cwd=invocation_cwd,
            expected=1,
        )
        self.assertIn("refusing to overwrite non-symlink", stderr)
        self.assertEqual((destination / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_skill_add_uses_windows_junction_materialization(self) -> None:
        invocation_cwd = self.root / "consumer"
        invocation_cwd.mkdir()

        with (
            mock.patch.object(
                workspace_skills_module, "is_windows_platform", return_value=True
            ),
            mock.patch.object(
                workspace_skills_module, "create_windows_junction"
            ) as create_junction,
        ):
            self.run_cli("skill", "add", "external-one", cwd=invocation_cwd)

        create_junction.assert_called_once_with(
            invocation_cwd / ".agents" / "skills" / "external-one",
            (self.root / "local-source" / "nested" / "external-one").resolve(),
        )

    def test_skill_list_default_scans_workspace_and_sources(self) -> None:
        stdout, stderr = self.run_cli("skill", "list")
        lines = stdout.strip().splitlines()

        self.assertEqual(lines[0], "source\tname\tselector\tpath")
        self.assertIn(
            f"workspace\tlocal-one\tskills/local-one\t{(self.root / 'skills' / 'local-one').resolve()}",
            lines,
        )
        self.assertIn(
            f"local-source\texternal-one\tnested/external-one\t{(self.root / 'local-source' / 'nested' / 'external-one').resolve()}",
            lines,
        )
        self.assertEqual(stderr, "")

        stdout, _stderr = self.run_cli("skill", "ls")
        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)

    def test_skill_list_source_filters(self) -> None:
        stdout, _stderr = self.run_cli("skill", "list", "--source", "workspace")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertNotIn("local-source\texternal-one", stdout)

        stdout, _stderr = self.run_cli("skill", "list", "-s", "local-source")

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("workspace\tlocal-one", stdout)

    def test_skill_list_multiple_source_filters_preserve_order_and_dedupe(self) -> None:
        stdout, _stderr = self.run_cli(
            "skill",
            "list",
            "-s",
            "local-source",
            "-s",
            "workspace",
            "-s",
            "local-source",
        )
        lines = stdout.strip().splitlines()

        self.assertEqual(lines[0], "source\tname\tselector\tpath")
        self.assertEqual(
            [line.split("\t")[0] for line in lines[1:]], ["local-source", "workspace"]
        )

    def test_skill_list_profile_applies_include_and_exclude(self) -> None:
        self.write_skill(self.root / "local-source" / "nested" / "external-two")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]
                include = ["skills/local-one"]

                [skill.local-source]
                include = ["*"]
                exclude = ["external-two"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("skill", "list", "--profile", "content")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("external-two", stdout)

    def test_skill_list_profile_and_source_filter_intersect(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.workspace]
                include = ["skills/local-one"]

                [skill.local-source]
                include = ["nested"]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli(
            "skill", "list", "-p", "content", "-s", "local-source"
        )

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertNotIn("workspace\tlocal-one", stdout)

    def test_skill_list_profile_can_show_duplicate_star_matches(self) -> None:
        self.write_skill(self.root / "local-source" / "other" / "external-one")
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [skill.local-source]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        stdout, _stderr = self.run_cli("skill", "list", "-p", "content")

        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertIn("local-source\texternal-one\tother/external-one", stdout)

    def test_skill_list_default_skips_missing_sources_with_warning(self) -> None:
        self.append_remote_source("remote-source")

        stdout, stderr = self.run_cli("skill", "list")

        self.assertIn("workspace\tlocal-one\tskills/local-one", stdout)
        self.assertIn("local-source\texternal-one\tnested/external-one", stdout)
        self.assertIn("Warning: skipping missing source remote-source:", stderr)

    def test_skill_list_rejects_unknown_missing_source_and_unknown_profile(
        self,
    ) -> None:
        _stdout, stderr = self.run_cli("skill", "list", "-s", "missing", expected=1)
        self.assertIn("unknown source: missing", stderr)

        self.append_remote_source("remote-source")
        _stdout, stderr = self.run_cli(
            "skill", "list", "-s", "remote-source", expected=1
        )
        self.assertIn("source path does not exist:", stderr)
        self.assertIn("run: hgc source sync remote-source", stderr)

        _stdout, stderr = self.run_cli("skill", "list", "-p", "missing", expected=1)
        self.assertIn("missing config:", stderr)

    def test_legacy_sources_schema_is_rejected(self) -> None:
        self.config_path.write_text(
            textwrap.dedent(
                """
                [[sources]]
                name = "old"
                path = "old"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli("source", "list", expected=1)
        self.assertIn("legacy [[sources]]", stderr)

    def test_legacy_skills_schema_is_rejected(self) -> None:
        profile_path = self.root / "profiles" / "content" / "config.toml"
        profile_path.write_text(
            textwrap.dedent(
                """
                name = "content"

                [[skills]]
                source = "local-source"
                """
            ).lstrip(),
            encoding="utf-8",
        )

        _stdout, stderr = self.run_cli(
            "profile", "apply", "-d", str(self.root / "target"), "content", expected=1
        )
        self.assertIn("legacy [[skills]]", stderr)

    def test_profile_skill_subcommand_is_not_registered(self) -> None:
        _stdout, stderr = self.run_cli("profile", "skill", expected=2)
        self.assertIn("No such command 'skill'", stderr)


if __name__ == "__main__":
    unittest.main()
