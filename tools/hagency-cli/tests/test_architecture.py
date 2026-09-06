from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from typer.main import get_command

from hagency_cli import cli
from hagency_cli.commands import file as file_commands
from hagency_cli.files.sync.models import SyncDirection
from hagency_cli.workspace.discovery import init_workspace
from hagency_cli.workspace.git import run
from hagency_cli.workspace.errors import WorkspaceError
from hagency_cli.workspace.operations.profiles import (
    add_profile,
    apply_profile_to_directory,
)
from hagency_cli.workspace.operations.skills import add_skills
from hagency_cli.workspace.operations.sources import add_source

PACKAGE = Path(cli.__file__).parent


def imported_modules(path: Path, tree: ast.AST) -> set[str]:
    """Resolve both absolute and relative imports for the dependency checks."""
    package = ("hagency_cli", *path.relative_to(PACKAGE).parent.parts)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            parent = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join(
                (*parent, *((node.module or "").split(".") if node.module else ()))
            )
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


class ArchitectureTests(unittest.TestCase):
    def test_subprocess_output_is_captured_and_forwarded_only_through_progress(self):
        code = "import sys; print('output'); print('diagnostic', file=sys.stderr)"
        events = []
        result = run([sys.executable, "-c", code], progress=events.append)
        self.assertEqual(result.stdout, "output\n")
        self.assertEqual(result.stderr, "diagnostic\n")
        self.assertIn(
            ("output", False), [(event.message, event.error) for event in events]
        )
        self.assertIn(
            ("diagnostic", True), [(event.message, event.error) for event in events]
        )
        with self.assertRaises(subprocess.CalledProcessError) as error:
            run([sys.executable, "-c", code + "; sys.exit(1)"])
        self.assertEqual(error.exception.stderr, "diagnostic\n")

    def test_business_packages_do_not_depend_on_terminal_or_commands(self):
        for package in ("workspace", "files"):
            for path in (PACKAGE / package).rglob("*.py"):
                with self.subTest(path=path.relative_to(PACKAGE)):
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                    imports = imported_modules(path, tree)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.Call):
                            if isinstance(node.func, ast.Name):
                                self.assertNotIn(
                                    node.func.id, {"print", "SystemExit", "__import__"}
                                )
                            elif isinstance(node.func, ast.Attribute):
                                self.assertNotIn(
                                    ast.unparse(node.func),
                                    {"sys.exit", "rich.print", "typer.echo"},
                                )
                    for name in imports:
                        self.assertFalse(
                            name.startswith(
                                (
                                    "typer",
                                    "questionary",
                                    "rich",
                                    "importlib",
                                    "hagency_cli.commands",
                                    "hagency_cli.cli",
                                )
                            ),
                            name,
                        )

    def test_workspace_dependencies_flow_from_profiles_to_skills_to_sources(self):
        for name, forbidden in (
            ("sources", {"skills", "profiles", "operations"}),
            ("skills", {"profiles", "operations"}),
            ("profiles", {"operations"}),
        ):
            tree = ast.parse((PACKAGE / "workspace" / f"{name}.py").read_text())
            imports = imported_modules(PACKAGE / "workspace" / f"{name}.py", tree)
            for dependency in forbidden:
                self.assertFalse(
                    any(
                        module.startswith(f"hagency_cli.workspace.{dependency}")
                        for module in imports
                    ),
                    (name, imports),
                )

    def test_workspace_import_graph_has_no_cycles(self):
        graph = {}
        for path in (PACKAGE / "workspace").rglob("*.py"):
            parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
            name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            graph[name] = imported_modules(path, ast.parse(path.read_text()))
        visited = set()

        def visit(name, trail):
            self.assertNotIn(name, trail, " -> ".join((*trail, name)))
            if name in visited:
                return
            for dependency in graph[name] & graph.keys():
                visit(dependency, (*trail, name))
            visited.add(name)

        for name in graph:
            visit(name, ())

    def test_normal_commands_and_help_do_not_load_optional_runtime_modules(self):
        code = """
import contextlib, io, json, sys
from hagency_cli.cli import app, main
from typer.main import get_command
def paths(command, prefix=()):
    yield prefix
    for name, child in getattr(command, 'commands', {}).items():
        yield from paths(child, (*prefix, name))
with contextlib.redirect_stdout(io.StringIO()):
    for path in paths(get_command(app)):
        try:
            main([*path, '--help'])
        except SystemExit as error:
            assert error.code == 0
print(json.dumps(sorted(sys.modules)))
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=directory,
                env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)},
                check=True,
                capture_output=True,
                text=True,
            )
        loaded = json.loads(result.stdout)
        for prefix in (
            "aiohttp",
            "paramiko",
            "questionary",
            "hagency_cli.model_proxy.server",
        ):
            self.assertFalse(
                any(name == prefix or name.startswith(prefix + ".") for name in loaded),
                prefix,
            )

    def test_workspace_operation_returns_a_result_and_is_silent_without_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = init_workspace(None, root, force=False, dry_run=False)
            self.assertEqual(result, root)
            self.assertTrue((result / "hagency-config.toml").is_file())
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))

    def test_source_skill_and_profile_success_and_failure_are_silent_without_progress(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "demo").mkdir(parents=True)
            (source / "demo/SKILL.md").write_text("fixture")
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                init_workspace(None, root, force=False, dry_run=False)
                name = add_source(
                    source_value="local",
                    name_value=None,
                    url_value=None,
                    path_value=str(source),
                    ref_value=None,
                    remote_name=None,
                    sync=False,
                    root_value=str(root),
                    dry_run=False,
                )
                skill_report = add_skills(
                    skill="local:demo",
                    cwd=root,
                    root_value=str(root),
                    skills_path=str(root / "skills"),
                )
                profile = add_profile(
                    name="profile",
                    description=None,
                    add_skill="local:demo",
                    include=None,
                    exclude=None,
                    root_value=str(root),
                    checkout_dir=None,
                    dry_run=False,
                )
                links = apply_profile_to_directory(
                    name="profile",
                    skills_path=str(root / "profile-copy"),
                    skills_root=None,
                    copy=True,
                    link_mode=None,
                    root_value=str(root),
                    checkout_dir=None,
                    dry_run=False,
                )
                with self.assertRaises(WorkspaceError):
                    add_skills(skill="unknown", cwd=root, root_value=str(root))
            self.assertEqual(name, "local")
            self.assertEqual(profile["name"], "profile")
            self.assertEqual(len(skill_report.selected), 1)
            self.assertEqual(len(links), 1)
            self.assertEqual(
                (root / "profile-copy/demo/SKILL.md").read_text(), "fixture"
            )
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))


class CommandTreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_main(self, *args, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        code = 0
        with (
            contextlib.chdir(self.root),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            try:
                cli.main(args)
            except SystemExit as error:
                code = error.code
        self.assertEqual(code, expected, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_public_tree_and_convenience_aliases(self):
        command = get_command(cli.app)
        self.assertEqual(
            set(command.commands),
            {"init", "source", "s", "skill", "profile", "p", "file", "service"},
        )
        expected = {
            "source": {"list", "ls", "show", "add", "remove", "rm", "sync"},
            "skill": {"list", "ls", "add"},
            "profile": {
                "list",
                "ls",
                "show",
                "add",
                "update",
                "u",
                "remove",
                "rm",
                "apply",
            },
            "file": {"init", "push", "pull", "sync", "pack", "apply", "purge"},
            "service": {"model-proxy"},
        }
        for name, commands in expected.items():
            self.assertEqual(set(command.commands[name].commands), commands)
            self.run_main(name, "--help")
        self.assertEqual(
            set(command.commands["service"].commands["model-proxy"].commands),
            {"start", "stop", "restart"},
        )
        self.assertEqual(set(command.commands["s"].commands), expected["source"])
        self.assertEqual(set(command.commands["p"].commands), expected["profile"])

    def test_old_entrypoints_are_rejected(self):
        cases = [
            (name,)
            for name in ("sync", "space", "serve", "push", "pull", "l2r", "r2l", "both")
        ]
        cases += [
            ("file", name)
            for name in ("l2r", "r2l", "both", "local-to-remote", "remote-to-local")
        ]
        cases += [(name, "init") for name in ("profile", "p")]
        cases += [
            ("service", "model-proxy", action, "--model-proxy")
            for action in ("start", "stop", "restart")
        ]
        cases += [("sync", "--help"), ("serve", "start"), ("service", "start")]
        cases += [("file", "push", "--exclude", "a", "b", "host:/remote")]
        cases += [
            ("file", "sync", option)
            for option in ("--delete", "--update", "--git-changed")
        ]
        for args in cases:
            with self.subTest(args=args):
                self.run_main(*args, expected=2)

    def test_main_preserves_remote_before_and_after_identity_and_exclude(self):
        directions = {
            "push": SyncDirection.LOCAL_TO_REMOTE,
            "pull": SyncDirection.REMOTE_TO_LOCAL,
            "sync": SyncDirection.BOTH,
        }
        for command, direction in directions.items():
            for endpoint in (
                "dev@host:/srv/project",
                "host:C:/Projects/ws",
                "[2001:db8::1]:/srv/project",
            ):
                for options in (
                    ("-i", "key", "--exclude", "*.tmp", "--exclude", "cache/"),
                    ("--identity=key", "--exclude=*.tmp", "--exclude=cache/"),
                ):
                    for args in ((endpoint, *options), (*options, endpoint)):
                        with (
                            self.subTest(command=command, args=args),
                            mock.patch.object(
                                file_commands,
                                "sync_workspace_files",
                                return_value=SimpleNamespace(actions=()),
                            ) as sync,
                        ):
                            self.run_main("file", command, *args, "--dry-run")
                            sync.assert_called_once()
                            self.assertEqual(
                                sync.call_args.args, (self.root, direction)
                            )
                            self.assertEqual(
                                sync.call_args.kwargs["remote_endpoint"], endpoint
                            )
                            self.assertEqual(
                                sync.call_args.kwargs["identity"], self.root / "key"
                            )
                            self.assertEqual(
                                sync.call_args.kwargs["exclude"], ["*.tmp", "cache/"]
                            )

    def test_legacy_multi_values_are_limited_to_profile_mutations(self):
        suffix = [
            "content",
            "-AS",
            "workspace",
            "-i",
            "one",
            "two",
            "--exclude=old",
            "draft",
        ]
        expanded = [
            "content",
            "-AS",
            "workspace",
            "-i",
            "one",
            "-i",
            "two",
            "--exclude=old",
            "--exclude",
            "draft",
        ]
        for group in ("profile", "p"):
            for action in ("add", "update", "u"):
                self.assertEqual(
                    cli.normalize_legacy_multi_value_options([group, action, *suffix]),
                    [group, action, *expanded],
                )
        for prefix in (
            ["file", "push"],
            ["file", "pull"],
            ["file", "sync"],
            ["skill", "add"],
            ["profile", "apply"],
        ):
            args = [*prefix, "--exclude", "*.tmp", "host:/remote"]
            self.assertEqual(cli.normalize_legacy_multi_value_options(args), args)
