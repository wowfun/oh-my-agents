from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


import hagency_cli.files.purge.models as files_purge_models_module
import hagency_cli.files.purge.operations as files_purge_operations_module
import hagency_cli.files.purge.removal as files_purge_removal_module
import hagency_cli.files.purge.roots as files_purge_roots_module
import hagency_cli.files.purge.scan as files_purge_scan_module
from hagency_cli.files.purge.models import (
    Activity,
    ItemDisposition,
    PurgeChoice,
    PurgeDisposition,
    PurgeRequest,
)
from hagency_cli.files.purge.operations import purge_space
from hagency_cli.files.purge.roots import purge_config_path


class FakePurgeUI:
    def __init__(
        self,
        *,
        interactive: bool,
        select: Callable[[tuple[PurgeChoice, ...]], tuple[str, ...] | None]
        | None = None,
        confirm: bool = False,
        confirm_hook: Callable[[tuple[Path, ...], int], bool] | None = None,
    ) -> None:
        self.interactive = interactive
        self.select_hook = select
        self.confirm = confirm
        self.confirm_hook = confirm_hook
        self.select_calls: list[tuple[PurgeChoice, ...]] = []
        self.confirm_calls: list[tuple[tuple[Path, ...], int]] = []

    def is_interactive(self) -> bool:
        return self.interactive

    def select(self, choices: tuple[PurgeChoice, ...]) -> tuple[str, ...] | None:
        self.select_calls.append(choices)
        if self.select_hook is None:
            return tuple(choice.id for choice in choices if choice.preselected)
        return self.select_hook(choices)

    def confirm_exact(self, paths: tuple[Path, ...], known_bytes: int) -> bool:
        self.confirm_calls.append((paths, known_bytes))
        if self.confirm_hook is not None:
            return self.confirm_hook(paths, known_bytes)
        return self.confirm


class SpacePurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_project(self, root: Path, name: str, marker: str = "package.json") -> Path:
        project = root / name
        project.mkdir(parents=True)
        (project / marker).write_text("{}\n", encoding="utf-8")
        return project

    def make_artifact(
        self,
        project: Path,
        name: str,
        *,
        size: int = 4096,
        old: bool = True,
    ) -> Path:
        artifact = project / name
        artifact.mkdir(parents=True)
        (artifact / "payload.bin").write_bytes(b"x" * size)
        if old:
            self.make_old(artifact)
        return artifact

    def make_old(self, path: Path) -> None:
        timestamp = time.time() - files_purge_models_module.MIN_AGE_SECONDS - 60
        descendants = sorted(
            path.rglob("*"), key=lambda child: len(child.parts), reverse=True
        )
        for child in descendants:
            os.utime(child, (timestamp, timestamp), follow_symlinks=False)
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)

    def all_choice_ids(self, choices: tuple[PurgeChoice, ...]) -> tuple[str, ...]:
        return tuple(choice.id for choice in choices)

    def test_platform_config_paths_and_fallbacks(self) -> None:
        home = self.base / "home"

        self.assertEqual(
            purge_config_path(
                environ={"XDG_CONFIG_HOME": "/tmp/xdg"}, platform="linux", home=home
            ),
            Path("/tmp/xdg/hagency/space-purge-paths"),
        )
        self.assertEqual(
            purge_config_path(
                environ={"XDG_CONFIG_HOME": "relative"}, platform="linux", home=home
            ),
            home / ".config/hagency/space-purge-paths",
        )
        self.assertEqual(
            purge_config_path(environ={}, platform="darwin", home=home),
            home / "Library/Application Support/Hagency/space-purge-paths",
        )
        self.assertEqual(
            purge_config_path(
                environ={"APPDATA": r"C:\Users\me\AppData\Roaming"},
                platform="win32",
                home=home,
            ),
            Path(r"C:\Users\me\AppData\Roaming") / "Hagency/space-purge-paths",
        )
        self.assertEqual(
            purge_config_path(environ={}, platform="win32", home=home),
            home / ".config/hagency/space-purge-paths",
        )

    def test_explicit_roots_scan_activity_preselection_and_sort_by_project_total(
        self,
    ) -> None:
        root = self.base / "scan"
        larger_project = self.make_project(root, "larger")
        smaller_project = self.make_project(root, "smaller")
        large = self.make_artifact(larger_project, "node_modules", size=192 * 1024)
        medium = self.make_artifact(larger_project, "dist", size=96 * 1024)
        small_project_artifact = self.make_artifact(
            smaller_project, "target", size=224 * 1024
        )
        recent = self.make_artifact(smaller_project, "build", size=4096, old=False)

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True),
            ui=FakePurgeUI(interactive=False),
        )

        self.assertEqual(report.roots, (root.resolve(),))
        self.assertEqual(
            [choice.exact_path for choice in report.choices],
            [
                large.resolve(),
                medium.resolve(),
                small_project_artifact.resolve(),
                recent.resolve(),
            ],
        )
        self.assertEqual(
            [choice.activity for choice in report.choices],
            [Activity.OLD, Activity.OLD, Activity.OLD, Activity.RECENT],
        )
        self.assertEqual(
            [choice.preselected for choice in report.choices], [True, True, True, False]
        )

    def test_tty_dry_run_selects_but_never_confirms_or_deletes(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        old = self.make_artifact(project, "node_modules")
        recent = self.make_artifact(project, "dist", old=False)
        ui = FakePurgeUI(interactive=True, select=self.all_choice_ids, confirm=True)

        report = purge_space(PurgeRequest(paths=(root,), dry_run=True), ui=ui)

        self.assertEqual(report.disposition, PurgeDisposition.PREVIEW)
        self.assertEqual(set(report.selected_paths), {old.resolve(), recent.resolve()})
        self.assertEqual(
            {result.disposition for result in report.results},
            {ItemDisposition.WOULD_REMOVE},
        )
        self.assertEqual(len(ui.select_calls), 1)
        self.assertEqual(ui.confirm_calls, [])
        self.assertTrue(old.exists())
        self.assertTrue(recent.exists())

    def test_real_run_requires_confirmation_and_removes_selected_artifact(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        artifact = self.make_artifact(project, "node_modules")
        rejecting_ui = FakePurgeUI(
            interactive=True, select=self.all_choice_ids, confirm=False
        )

        cancelled = purge_space(PurgeRequest(paths=(root,)), ui=rejecting_ui)

        self.assertEqual(cancelled.disposition, PurgeDisposition.CANCELLED)
        self.assertTrue(artifact.exists())
        self.assertEqual(rejecting_ui.confirm_calls[0][0], (artifact.resolve(),))
        self.assertGreater(rejecting_ui.confirm_calls[0][1], 0)

        accepting_ui = FakePurgeUI(
            interactive=True, select=self.all_choice_ids, confirm=True
        )
        completed = purge_space(PurgeRequest(paths=(root,)), ui=accepting_ui)

        self.assertEqual(completed.disposition, PurgeDisposition.COMPLETED)
        self.assertEqual(completed.exit_code, 0)
        self.assertEqual(completed.results[0].disposition, ItemDisposition.REMOVED)
        self.assertFalse(artifact.exists())

    def test_non_tty_is_always_read_only_and_only_marks_defaults_selected(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        old = self.make_artifact(project, "node_modules")
        recent = self.make_artifact(project, "dist", old=False)
        ui = FakePurgeUI(interactive=False, confirm=True)

        report = purge_space(PurgeRequest(paths=(root,), dry_run=False), ui=ui)

        self.assertEqual(report.disposition, PurgeDisposition.PREVIEW)
        self.assertEqual(report.selected_paths, (old.resolve(),))
        self.assertEqual(
            [result.exact_path for result in report.results], [old.resolve()]
        )
        self.assertEqual(ui.select_calls, [])
        self.assertEqual(ui.confirm_calls, [])
        self.assertTrue(old.exists())
        self.assertTrue(recent.exists())

    @unittest.skipUnless(shutil.which("git"), "git is required for tracked-file safety")
    def test_git_tracked_candidate_is_excluded(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        subprocess.run(
            ["git", "init", "-q", str(project)],
            check=True,
            capture_output=True,
        )
        artifact = self.make_artifact(project, "node_modules")
        subprocess.run(
            ["git", "-C", str(project), "add", "-f", "node_modules/payload.bin"],
            check=True,
            capture_output=True,
        )

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True), ui=FakePurgeUI(interactive=False)
        )

        self.assertEqual(report.choices, ())
        self.assertEqual(report.issues, ())
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(artifact.exists())

    def test_git_query_failure_fails_closed(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        (project / ".git").mkdir()
        artifact = self.make_artifact(project, "node_modules")
        failed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout=b"", stderr=b"not a repository"
        )

        with mock.patch.object(
            files_purge_roots_module.subprocess, "run", return_value=failed
        ):
            report = purge_space(
                PurgeRequest(paths=(root,), dry_run=True),
                ui=FakePurgeUI(interactive=False),
            )

        self.assertEqual(report.choices, ())
        self.assertEqual([issue.code for issue in report.issues], ["git_check_failed"])
        self.assertEqual(report.disposition, PurgeDisposition.PREVIEW)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(artifact.exists())

    def test_symlink_named_like_artifact_is_ignored(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks are not supported")
        root = self.base / "scan"
        project = self.make_project(root, "project")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep", encoding="utf-8")
        try:
            (project / "node_modules").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"could not create symlink: {exc}")

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True), ui=FakePurgeUI(interactive=False)
        )

        self.assertEqual(report.choices, ())
        self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_candidate_identity_replacement_is_skipped_and_fails_report(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        artifact = self.make_artifact(project, "node_modules")
        moved = project / "old-node-modules"

        def replace_before_confirmation(
            paths: tuple[Path, ...], _known_bytes: int
        ) -> bool:
            self.assertEqual(paths, (artifact.resolve(),))
            artifact.rename(moved)
            artifact.mkdir()
            (artifact / "do-not-delete.txt").write_text("new", encoding="utf-8")
            return True

        ui = FakePurgeUI(
            interactive=True,
            select=self.all_choice_ids,
            confirm_hook=replace_before_confirmation,
        )

        report = purge_space(PurgeRequest(paths=(root,)), ui=ui)

        self.assertEqual(report.disposition, PurgeDisposition.PARTIAL)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertIn("changed", report.results[0].message)
        self.assertEqual(
            (artifact / "do-not-delete.txt").read_text(encoding="utf-8"), "new"
        )
        self.assertTrue(moved.exists())

    def test_nonempty_config_overrides_autodiscovery(self) -> None:
        home = self.base / "home"
        automatic_root = home / "Projects"
        configured_root = home / "configured"
        automatic = self.make_artifact(
            self.make_project(automatic_root, "auto-project"), "node_modules"
        )
        configured = self.make_artifact(
            self.make_project(configured_root, "configured-project"), "target"
        )
        xdg = home / "config"
        config_path = xdg / "hagency/space-purge-paths"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            f"# configured roots\n{configured_root}\n", encoding="utf-8"
        )

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
        ):
            report = purge_space(
                PurgeRequest(dry_run=True), ui=FakePurgeUI(interactive=False)
            )

        self.assertEqual(report.roots, (configured_root.resolve(),))
        self.assertEqual(
            [choice.exact_path for choice in report.choices], [configured.resolve()]
        )
        self.assertNotIn(
            automatic.resolve(), [choice.exact_path for choice in report.choices]
        )

    def test_empty_config_uses_standard_and_dynamic_autodiscovery(self) -> None:
        home = self.base / "home"
        standard_root = home / "Projects"
        standard = self.make_artifact(
            self.make_project(standard_root, "standard-project"), "node_modules"
        )
        dynamic_root = home / "ClientWork"
        dynamic = self.make_artifact(
            self.make_project(dynamic_root, "nested/project"), "target"
        )
        xdg = home / "config"
        config_path = xdg / "hagency/space-purge-paths"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("# no configured paths\n", encoding="utf-8")

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
        ):
            report = purge_space(
                PurgeRequest(dry_run=True), ui=FakePurgeUI(interactive=False)
            )

        self.assertIn(standard_root.resolve(), report.roots)
        self.assertIn(dynamic_root.resolve(), report.roots)
        self.assertEqual(
            {choice.exact_path for choice in report.choices},
            {standard.resolve(), dynamic.resolve()},
        )

    def test_explicit_path_overrides_configured_path(self) -> None:
        home = self.base / "home"
        configured_root = home / "configured"
        configured = self.make_artifact(
            self.make_project(configured_root, "configured-project"), "node_modules"
        )
        explicit_root = self.base / "explicit"
        explicit = self.make_artifact(
            self.make_project(explicit_root, "explicit-project"), "target"
        )
        xdg = home / "config"
        config_path = xdg / "hagency/space-purge-paths"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(f"{configured_root}\n", encoding="utf-8")

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
        ):
            report = purge_space(
                PurgeRequest(paths=(explicit_root,), dry_run=True),
                ui=FakePurgeUI(interactive=False),
            )

        self.assertEqual(report.roots, (explicit_root.resolve(),))
        self.assertEqual(
            [choice.exact_path for choice in report.choices], [explicit.resolve()]
        )
        self.assertNotIn(
            configured.resolve(), [choice.exact_path for choice in report.choices]
        )

    def test_valid_cachedir_tag_is_candidate_but_invalid_signature_is_not(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        valid = project / "custom-cache"
        valid.mkdir()
        (valid / files_purge_models_module.CACHEDIR_TAG_NAME).write_bytes(
            files_purge_models_module.CACHEDIR_TAG_SIGNATURE + b"\n"
        )
        (valid / "payload.bin").write_bytes(b"x" * 4096)
        self.make_old(valid)
        invalid = project / "not-a-cache"
        invalid.mkdir()
        (invalid / files_purge_models_module.CACHEDIR_TAG_NAME).write_text(
            "invalid signature\n", encoding="utf-8"
        )
        (invalid / "payload.bin").write_bytes(b"x" * 4096)
        self.make_old(invalid)

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True), ui=FakePurgeUI(interactive=False)
        )

        self.assertEqual(
            [choice.exact_path for choice in report.choices], [valid.resolve()]
        )
        self.assertEqual(
            report.choices[0].artifact_kind, files_purge_models_module.CACHEDIR_TAG_NAME
        )

    def test_vendor_and_bin_require_matching_project_context(self) -> None:
        root = self.base / "scan"
        composer = self.make_project(root, "composer", marker="composer.json")
        allowed_vendor = self.make_artifact(composer, "vendor")
        node = self.make_project(root, "node")
        rejected_vendor = self.make_artifact(node, "vendor")
        rejected_bin = self.make_artifact(node, "bin")
        dotnet = self.make_project(root, "dotnet")
        (dotnet / "app.csproj").write_text("<Project />\n", encoding="utf-8")
        allowed_bin = dotnet / "bin"
        (allowed_bin / "Debug").mkdir(parents=True)
        (allowed_bin / "Debug/app.dll").write_bytes(b"x" * 4096)
        self.make_old(allowed_bin)

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True), ui=FakePurgeUI(interactive=False)
        )

        paths = {choice.exact_path for choice in report.choices}
        self.assertIn(allowed_vendor.resolve(), paths)
        self.assertIn(allowed_bin.resolve(), paths)
        self.assertNotIn(rejected_vendor.resolve(), paths)
        self.assertNotIn(rejected_bin.resolve(), paths)

    def test_root_home_and_symlink_roots_are_rejected(self) -> None:
        home = self.base / "home"
        home.mkdir()
        real_root = self.base / "real-root"
        artifact = self.make_artifact(
            self.make_project(real_root, "project"), "node_modules"
        )
        link_root = self.base / "linked-root"
        try:
            link_root.symlink_to(real_root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"could not create symlink: {exc}")
        real_container = self.base / "real-container"
        nested_root = real_container / "nested"
        nested_artifact = self.make_artifact(
            self.make_project(nested_root, "project"), "target"
        )
        linked_container = self.base / "linked-container"
        linked_container.symlink_to(real_container, target_is_directory=True)
        linked_nested_root = linked_container / "nested"

        with mock.patch.object(
            files_purge_roots_module.Path, "home", return_value=home
        ):
            home_report = purge_space(
                PurgeRequest(paths=(home,)), ui=FakePurgeUI(interactive=False)
            )
            root_report = purge_space(
                PurgeRequest(paths=(Path(home.anchor),)),
                ui=FakePurgeUI(interactive=False),
            )
            link_report = purge_space(
                PurgeRequest(paths=(link_root,)),
                ui=FakePurgeUI(interactive=False),
            )
            ancestor_link_report = purge_space(
                PurgeRequest(paths=(linked_nested_root,)),
                ui=FakePurgeUI(interactive=False),
            )

        for report in (
            home_report,
            root_report,
            link_report,
            ancestor_link_report,
        ):
            self.assertEqual(report.exit_code, 1)
            self.assertEqual(report.choices, ())
            self.assertEqual([issue.code for issue in report.issues], ["invalid_root"])
        self.assertTrue(artifact.exists())
        self.assertTrue(nested_artifact.exists())

    @unittest.skipUnless(shutil.which("git"), "git is required for tracked-file safety")
    def test_git_repo_above_scan_root_and_nested_repo_are_protected(self) -> None:
        repository = self.base / "repository"
        repository.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(repository)],
            check=True,
            capture_output=True,
        )
        scan_root = repository / "packages"
        tracked_project = self.make_project(scan_root, ":(glob)project")
        tracked_artifact = self.make_artifact(tracked_project, "dist")
        subprocess.run(
            ["git", "-C", str(repository), "add", "-f", "."],
            check=True,
            capture_output=True,
        )

        outer = self.make_project(self.base / "other-scan", "outer")
        nested_repo = self.make_artifact(outer, "build")
        subprocess.run(
            ["git", "init", "-q", str(nested_repo)],
            check=True,
            capture_output=True,
        )
        (nested_repo / "source.py").write_text("print('keep')\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(nested_repo), "add", "source.py"],
            check=True,
            capture_output=True,
        )
        self.make_old(nested_repo)

        with mock.patch.dict(
            os.environ, {"GIT_DIR": str(self.base / "wrong-git-dir")}, clear=False
        ):
            report = purge_space(
                PurgeRequest(paths=(scan_root, outer.parent), dry_run=True),
                ui=FakePurgeUI(interactive=False),
            )

        self.assertEqual(report.choices, ())
        self.assertEqual(report.issues, ())
        self.assertTrue(tracked_artifact.exists())
        self.assertTrue(nested_repo.exists())

    def test_cachedir_tag_removed_after_confirmation_is_skipped(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        candidate = project / "custom-cache"
        candidate.mkdir()
        tag = candidate / files_purge_models_module.CACHEDIR_TAG_NAME
        tag.write_bytes(files_purge_models_module.CACHEDIR_TAG_SIGNATURE + b"\n")
        (candidate / "payload.bin").write_bytes(b"x" * 4096)
        self.make_old(candidate)

        def invalidate_tag(_paths: tuple[Path, ...], _known_bytes: int) -> bool:
            tag.unlink()
            self.make_old(candidate)
            return True

        report = purge_space(
            PurgeRequest(paths=(root,)),
            ui=FakePurgeUI(
                interactive=True,
                select=self.all_choice_ids,
                confirm_hook=invalidate_tag,
            ),
        )

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertIn("CACHEDIR.TAG", report.results[0].message)
        self.assertTrue(candidate.exists())

    def test_depth_bounds_catalog_and_overlapping_roots(self) -> None:
        project = self.base / "project"
        project.mkdir()
        (project / "package.json").write_text("{}\n", encoding="utf-8")
        (project / "composer.json").write_text("{}\n", encoding="utf-8")
        (project / "app.csproj").write_text("<Project />\n", encoding="utf-8")

        for name in sorted(files_purge_models_module.PURGE_TARGETS):
            artifact = project / name
            if name == "bin":
                (artifact / "Debug").mkdir(parents=True)
                payload_parent = artifact / "Debug"
            else:
                artifact.mkdir()
                payload_parent = artifact
            (payload_parent / "payload.bin").write_bytes(b"x" * 4096)
            self.make_old(artifact)

        deep_six = project / "one/two/three/four/five/.cache-six"
        deep_six.mkdir(parents=True)
        (deep_six / files_purge_models_module.CACHEDIR_TAG_NAME).write_bytes(
            files_purge_models_module.CACHEDIR_TAG_SIGNATURE
        )
        (deep_six / "payload.bin").write_bytes(b"x" * 4096)
        self.make_old(deep_six)
        too_deep = project / "a/b/c/d/e/f/.cache-seven"
        too_deep.mkdir(parents=True)
        (too_deep / files_purge_models_module.CACHEDIR_TAG_NAME).write_bytes(
            files_purge_models_module.CACHEDIR_TAG_SIGNATURE
        )
        (too_deep / "payload.bin").write_bytes(b"x" * 4096)
        self.make_old(too_deep)

        report = purge_space(
            PurgeRequest(paths=(project, project.parent), dry_run=True),
            ui=FakePurgeUI(interactive=False),
        )

        named = {
            choice.artifact_kind
            for choice in report.choices
            if choice.artifact_kind != files_purge_models_module.CACHEDIR_TAG_NAME
        }
        self.assertEqual(named, set(files_purge_models_module.PURGE_TARGETS))
        paths = [choice.exact_path for choice in report.choices]
        self.assertEqual(paths.count((project / "node_modules").resolve()), 1)
        self.assertIn(deep_six.resolve(), paths)
        self.assertNotIn(too_deep.resolve(), paths)

    def test_hardlinks_are_deduplicated_in_selected_total(self) -> None:
        if not hasattr(os, "link"):
            self.skipTest("hard links are not supported")
        root = self.base / "scan"
        project = self.make_project(root, "project")
        build = self.make_artifact(project, "build", size=64 * 1024)
        dist = self.make_artifact(project, "dist", size=1)
        (dist / "payload.bin").unlink()
        try:
            os.link(build / "payload.bin", dist / "payload.bin")
        except OSError as exc:
            self.skipTest(f"could not create hard link: {exc}")
        self.make_old(dist)

        report = purge_space(
            PurgeRequest(paths=(root,), dry_run=True),
            ui=FakePurgeUI(interactive=False),
        )

        standalone_total = sum(choice.size_bytes or 0 for choice in report.choices)
        self.assertEqual(len(report.choices), 2)
        self.assertLess(report.known_bytes, standalone_total)
        self.assertGreater(report.known_bytes, 0)

    def test_project_sort_totals_dedupe_hardlinks_across_projects(self) -> None:
        root = self.base / "scan"
        projects = {name: self.make_project(root, name) for name in ("a", "b", "c")}
        for project in projects.values():
            self.make_artifact(project, "build")

        shared = files_purge_models_module._HardlinkEntry((99, 101), 100)

        def measured(path: Path, _now: float):
            if path.parent.name in {"a", "b"}:
                return 100, Activity.OLD, None, (shared,)
            return 60, Activity.OLD, None, ()

        with mock.patch.object(
            files_purge_scan_module, "_measure_candidate", side_effect=measured
        ):
            report = purge_space(
                PurgeRequest(paths=(root,), dry_run=True),
                ui=FakePurgeUI(interactive=False),
            )

        self.assertEqual(
            [choice.project_path.name for choice in report.choices],
            ["a", "c", "b"],
        )

    def test_measurement_failure_after_confirmation_prevents_removal(self) -> None:
        root = self.base / "scan"
        artifact = self.make_artifact(self.make_project(root, "project"), "build")
        ui = FakePurgeUI(interactive=True, select=self.all_choice_ids, confirm=True)
        with mock.patch.object(
            files_purge_removal_module,
            "_measure_candidate",
            return_value=(None, Activity.UNCERTAIN, "injected read failure", ()),
        ) as measure:
            report = purge_space(PurgeRequest(paths=(root,)), ui=ui)
        self.assertEqual(len(ui.confirm_calls), 1)
        measure.assert_called_once()
        self.assertTrue(artifact.exists())
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any("activity safety check failed" in str(item) for item in report.results)
        )

    def test_incomplete_discovery_and_measurement_exit_one(self) -> None:
        home = self.base / "home"
        home.mkdir()
        real_scandir = files_purge_roots_module.os.scandir

        def fail_home(path: os.PathLike[str] | str):
            if Path(path) == home:
                raise PermissionError("denied")
            return real_scandir(path)

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.object(
                files_purge_roots_module.os, "scandir", side_effect=fail_home
            ),
        ):
            discovery = purge_space(
                PurgeRequest(dry_run=True), ui=FakePurgeUI(interactive=False)
            )

        self.assertEqual(discovery.disposition, PurgeDisposition.PREVIEW)
        self.assertEqual(discovery.exit_code, 1)
        self.assertEqual(
            [issue.code for issue in discovery.issues], ["discovery_scan_failed"]
        )

        root = self.base / "scan"
        self.make_artifact(self.make_project(root, "project"), "target")
        with mock.patch.object(
            files_purge_scan_module,
            "_measure_candidate",
            return_value=(None, Activity.UNCERTAIN, "permission denied", ()),
        ):
            measurement = purge_space(
                PurgeRequest(paths=(root,), dry_run=True),
                ui=FakePurgeUI(interactive=False),
            )

        self.assertEqual(measurement.exit_code, 1)
        self.assertEqual(measurement.choices[0].activity, Activity.UNCERTAIN)
        self.assertFalse(measurement.choices[0].preselected)
        self.assertIn("candidate_measure_failed", [i.code for i in measurement.issues])

    def test_disappearing_candidate_and_partial_delete_fail_safely(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        first = self.make_artifact(project, "build", size=8192)
        second = self.make_artifact(project, "dist", size=4096)

        def disappear(paths: tuple[Path, ...], _known_bytes: int) -> bool:
            shutil.rmtree(paths[0])
            return True

        disappeared = purge_space(
            PurgeRequest(paths=(root,)),
            ui=FakePurgeUI(
                interactive=True,
                select=lambda choices: (choices[0].id,),
                confirm_hook=disappear,
            ),
        )
        self.assertEqual(disappeared.exit_code, 1)
        self.assertEqual(disappeared.results[0].disposition, ItemDisposition.SKIPPED)

        if not first.exists():
            first = self.make_artifact(project, "build", size=8192)
        if not second.exists():
            second = self.make_artifact(project, "dist", size=4096)
        original_remove = files_purge_removal_module._permanently_remove

        def fail_one(candidate: object) -> None:
            if candidate.choice.exact_path.name == "build":
                raise PermissionError("denied")
            original_remove(candidate)

        with mock.patch.object(
            files_purge_operations_module, "_permanently_remove", side_effect=fail_one
        ):
            partial = purge_space(
                PurgeRequest(paths=(root,)),
                ui=FakePurgeUI(
                    interactive=True,
                    select=self.all_choice_ids,
                    confirm=True,
                ),
            )

        self.assertEqual(partial.exit_code, 1)
        self.assertEqual(partial.disposition, PurgeDisposition.PARTIAL)
        self.assertEqual(
            {result.disposition for result in partial.results},
            {ItemDisposition.FAILED, ItemDisposition.REMOVED},
        )
        self.assertTrue((project / "build").exists())
        self.assertFalse((project / "dist").exists())

    def test_root_and_parent_replacement_are_skipped(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        artifact = self.make_artifact(project, "node_modules")
        moved_root = self.base / "moved-scan"

        def replace_root(_paths: tuple[Path, ...], _known_bytes: int) -> bool:
            root.rename(moved_root)
            replacement = self.make_project(root, "project")
            self.make_artifact(replacement, "node_modules")
            return True

        root_report = purge_space(
            PurgeRequest(paths=(root,)),
            ui=FakePurgeUI(
                interactive=True,
                select=self.all_choice_ids,
                confirm_hook=replace_root,
            ),
        )

        self.assertEqual(root_report.exit_code, 1)
        self.assertEqual(root_report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue((root / "project/node_modules").exists())
        self.assertTrue((moved_root / "project/node_modules").exists())

        shutil.rmtree(root)
        moved_root.rename(root)
        moved_project = root / "moved-project"

        def replace_parent(_paths: tuple[Path, ...], _known_bytes: int) -> bool:
            project.rename(moved_project)
            replacement = self.make_project(root, "project")
            self.make_artifact(replacement, "node_modules")
            return True

        parent_report = purge_space(
            PurgeRequest(paths=(root,)),
            ui=FakePurgeUI(
                interactive=True,
                select=lambda choices: (
                    next(
                        choice.id
                        for choice in choices
                        if choice.exact_path == artifact.resolve()
                    ),
                ),
                confirm_hook=replace_parent,
            ),
        )

        self.assertEqual(parent_report.exit_code, 1)
        self.assertEqual(parent_report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertTrue((root / "project/node_modules").exists())
        self.assertTrue((moved_project / "node_modules").exists())

    def test_automatic_discovery_excludes_cloud_and_system_home_dirs(self) -> None:
        home = self.base / "home"
        allowed_root = home / "ClientWork"
        allowed = self.make_artifact(
            self.make_project(allowed_root, "project"), "node_modules"
        )
        excluded: list[Path] = []
        for name in (
            "Dropbox (Acme)",
            "onedrive - Acme",
            "Nextcloud",
            "Box",
            "Documents",
        ):
            artifact = self.make_artifact(
                self.make_project(home / name, "project"), "target"
            )
            excluded.append(artifact)
        xdg = home / "config"

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}, clear=False),
        ):
            report = purge_space(
                PurgeRequest(dry_run=True), ui=FakePurgeUI(interactive=False)
            )

        self.assertIn(allowed.resolve(), [c.exact_path for c in report.choices])
        discovered_paths = {choice.exact_path for choice in report.choices}
        self.assertTrue(
            all(path.resolve() not in discovered_paths for path in excluded)
        )

    def test_paths_editor_creates_template_uses_visual_and_reloads(self) -> None:
        home = self.base / "home"
        configured_root = home / "Work"
        self.make_project(configured_root, "project")
        xdg = home / "config"
        expected_config = xdg / "hagency/space-purge-paths"

        def edit_config(
            command: list[str], *, check: bool
        ) -> subprocess.CompletedProcess:
            self.assertFalse(check)
            self.assertEqual(command, ["code", "--wait", str(expected_config)])
            expected_config.write_text(f"{configured_root}\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with (
            mock.patch.object(files_purge_roots_module.Path, "home", return_value=home),
            mock.patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(xdg),
                    "VISUAL": "code --wait",
                    "EDITOR": "vi",
                },
                clear=True,
            ),
            mock.patch.object(
                files_purge_roots_module.subprocess, "run", side_effect=edit_config
            ),
        ):
            report = files_purge_roots_module.edit_purge_paths()

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.config_path, expected_config)
        self.assertEqual(report.editor, "code --wait")
        self.assertEqual(report.after_roots, (configured_root.resolve(),))
        self.assertTrue(expected_config.exists())

        with mock.patch.object(files_purge_roots_module.sys, "platform", "win32"):
            command = files_purge_roots_module._split_editor_command(
                '"C:\\Program Files\\Editor\\editor.exe" --wait'
            )
        self.assertEqual(
            command,
            [r"C:\Program Files\Editor\editor.exe", "--wait"],
        )

    def test_config_template_does_not_clobber_concurrently_created_file(self) -> None:
        config_path = self.base / "config" / "hagency" / "space-purge-paths"
        original_mkdir = Path.mkdir
        raced = False

        def racing_mkdir(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal raced
            original_mkdir(path, *args, **kwargs)
            if path == config_path.parent and not raced:
                raced = True
                config_path.write_text("user-created\n", encoding="utf-8")

        with mock.patch.object(Path, "mkdir", racing_mkdir):
            files_purge_roots_module._write_config_template(config_path)

        self.assertEqual(config_path.read_text(encoding="utf-8"), "user-created\n")

    def test_nested_mount_candidate_is_skipped_before_deletion(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        artifact = self.make_artifact(project, "build")
        nested_mount = artifact / "mounted"
        nested_mount.mkdir()
        (nested_mount / "keep.txt").write_text("keep\n", encoding="utf-8")
        self.make_old(artifact)

        with mock.patch.object(
            files_purge_roots_module.os.path,
            "ismount",
            side_effect=lambda path: Path(path) == nested_mount,
        ):
            report = purge_space(
                PurgeRequest(paths=(root,)),
                ui=FakePurgeUI(
                    interactive=True,
                    select=self.all_choice_ids,
                    confirm=True,
                ),
            )

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.results[0].disposition, ItemDisposition.SKIPPED)
        self.assertIn("mount point", report.results[0].message)
        self.assertTrue((nested_mount / "keep.txt").exists())

    def test_deep_artifact_tree_deletes_without_python_recursion(self) -> None:
        root = self.base / "scan"
        project = self.make_project(root, "project")
        artifact = self.make_artifact(project, "build")
        deepest = artifact
        for _index in range(180):
            deepest /= "d"
            deepest.mkdir()
        (deepest / "leaf.txt").write_text("leaf", encoding="utf-8")
        self.make_old(artifact)

        previous_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(100)
            report = purge_space(
                PurgeRequest(paths=(root,)),
                ui=FakePurgeUI(
                    interactive=True,
                    select=self.all_choice_ids,
                    confirm=True,
                ),
            )
        finally:
            sys.setrecursionlimit(previous_limit)

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.results[0].disposition, ItemDisposition.REMOVED)
        self.assertFalse(artifact.exists())


if __name__ == "__main__":
    unittest.main()
