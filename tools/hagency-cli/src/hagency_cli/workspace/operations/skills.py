"""Install selected skills through the workspace's persistent source model."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hagency_cli.workspace.config import write_toml
from hagency_cli.workspace.context import default_sync_depth, load_registry
from hagency_cli.workspace.discovery import (
    resolve_workspace_root,
    workspace_config_path,
)
from hagency_cli.workspace.errors import (
    GitCommandError,
    SourceNotReadyError,
    WorkspaceError,
    fail,
    format_called_process_error,
)
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.skills import (
    SkillConflictUI,
    SkillLinkCandidate,
    validate_skills_dir,
    default_link_mode,
    discover_skill_dirs,
    install_skill,
    resolve_link_name_conflicts,
    resolve_selector,
    resolve_skill_install_dir,
    resolve_skill_reference,
    skill_skip_roots,
    skill_source,
    validate_skill_selector,
    workspace_source,
)
from hagency_cli.workspace.source_inputs import (
    SourceSelection,
    classify_skill_input,
    resolve_local_input,
    resolve_remote_input,
)
from hagency_cli.workspace.sources import (
    Source,
    SourceSyncError,
    add_source_entry,
    require_source_path,
    resolve_sources,
    sync_source,
)


class SkillSelectionUI(SkillConflictUI, Protocol):
    def choose_skills(
        self, candidates: tuple[SkillLinkCandidate, ...]
    ) -> tuple[Path, ...] | None: ...


@dataclass(frozen=True)
class SkillAddReport:
    source: Source
    registered: bool
    selected: tuple[SkillLinkCandidate, ...]
    destination: Path
    dry_run: bool
    provisional: bool = False


def _check_requested_ref(source: Source, ref: str) -> None:
    if source.remote is None or source.remote.ref != ref:
        fail(
            f"source {source.name} does not match --ref {ref!r}; existing sources are never retargeted"
        )
    try:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=source.path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if branch.returncode == 0 and branch.stdout.strip() == ref.removeprefix(
            "refs/heads/"
        ):
            return
        # Tags are valid clone refs and yield detached HEADs. Do not accept a
        # different checked-out branch just because its commit happens to match.
        if branch.returncode != 0:
            tag = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/tags/{ref}^{{commit}}"],
                cwd=source.path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            head = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=source.path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if (
                tag.returncode == head.returncode == 0
                and tag.stdout.strip() == head.stdout.strip()
            ):
                return
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"cannot inspect source {source.name} ref: {exc}")
    raise SourceNotReadyError(
        f"source {source.name} is not checked out at {ref!r}", (source.name,)
    )


def _select_candidates(
    selection: SourceSelection,
    *,
    selectors: tuple[str, ...],
    all_skills: bool,
    exact: str | None,
    sources: dict[str, Source],
    ui: SkillSelectionUI | None,
    dry_run: bool,
    progress: Progress | None,
) -> tuple[SkillLinkCandidate, ...]:
    source = selection.source
    skip = skill_skip_roots(source.name, sources)
    if exact is not None:
        links = resolve_selector(source, exact, skip_roots=skip)
        if len(links) != 1:
            fail(
                f"skill reference {source.name + ':' + exact!r} matched {len(links)} skills; choose one exact SOURCE:selector"
            )
        return (SkillLinkCandidate(links[0][0], source.name, links[0][1]),)
    scope = selection.scope_root.resolve()
    available = tuple(
        SkillLinkCandidate(path.name, source.name, path)
        for path in discover_skill_dirs(scope, skip_roots=skip)
    )
    if not available:
        fail(f"no SKILL.md files found in source {source.name}: {scope}")
    if selectors:
        selected = []
        allowed = {candidate.target.resolve() for candidate in available}
        for selector in selectors:
            for name, path in resolve_selector(
                source, selector, skip_roots=skip, allow_name_conflicts=True
            ):
                if path.resolve() not in allowed:
                    fail(
                        f"skill selector {selector!r} is outside the input directory: {scope}"
                    )
                selected.append(SkillLinkCandidate(name, source.name, path))
    elif all_skills or len(available) == 1:
        selected = list(available)
    elif dry_run:
        for candidate in available:
            emit_event(
                progress, f"candidate {candidate.source_name}: {candidate.target}"
            )
        emit_event(
            progress,
            "Multiple skills require --skill/--all or interactive selection; installation selection is not yet verified",
        )
        return ()
    else:
        if ui is None or not ui.is_interactive():
            choices = ", ".join(
                str(c.target.relative_to(source.path.resolve())) for c in available
            )
            fail(
                f"multiple skills found: {choices}; use --skill SELECTOR (repeatable) or --all"
            )
        try:
            paths = ui.choose_skills(available)
        except (OSError, RuntimeError) as exc:
            fail(f"interactive skill selection failed: {exc}")
        if not paths:
            fail(
                "skill selection cancelled; source checkout and registration are retained"
            )
        chosen = {p.resolve() for p in paths}
        if not chosen.issubset({c.target.resolve() for c in available}):
            fail("invalid interactive skill selection")
        selected = [c for c in available if c.target.resolve() in chosen]
    return tuple(
        resolve_link_name_conflicts(
            selected, ui, preview_conflicts=dry_run, progress=progress
        )
    )


def add_skills(
    *,
    skill: str,
    skills_path: str | None = None,
    skills_root: str | None = None,
    global_install: bool = False,
    root_value: str | None = None,
    checkout_dir: str | None = None,
    dry_run: bool = False,
    selectors: tuple[str, ...] = (),
    all_skills: bool = False,
    source_name: str | None = None,
    ref: str | None = None,
    cwd: Path,
    ui: SkillSelectionUI | None = None,
    progress: Progress | None = None,
) -> SkillAddReport:
    if selectors and all_skills:
        fail("--skill and --all are mutually exclusive")
    if any(not selector for selector in selectors):
        fail("--skill cannot be empty")
    if ref is not None and (
        not ref or ref.startswith("-") or any(c.isspace() for c in ref)
    ):
        fail("--ref must be a nonempty Git branch or tag")
    root = resolve_workspace_root(root_value, cwd)
    registry = load_registry(root)
    sources = resolve_sources(registry, repo_root=root, checkout_override=checkout_dir)
    destination = resolve_skill_install_dir(
        skills_path, skills_root, global_install, cwd, default_root=cwd
    )
    validate_skills_dir(destination)
    parsed = classify_skill_input(skill, sources)
    exact = parsed.selector
    if parsed.kind in {"selector", "name"} and (selectors or all_skills):
        fail("an exact skill reference cannot be combined with --skill or --all")
    if parsed.kind in {"source", "selector", "name"}:
        if source_name is not None:
            fail("--source-name is only supported with a URL or local path")
        if parsed.kind == "name":
            name, exact = resolve_skill_reference(skill, sources, root)
        else:
            name = parsed.value
        source = skill_source(name, sources, workspace_source(root))
        selection = SourceSelection(source, source.path)
    elif parsed.kind == "url":
        selection = resolve_remote_input(
            parsed.value,
            source_name=source_name,
            ref=ref,
            sources=sources,
            registry=registry,
            root=root,
            checkout_dir=checkout_dir,
        )
    else:
        selection = resolve_local_input(
            parsed.value,
            cwd=cwd,
            source_name=source_name,
            sources=sources,
            registry=registry,
            root=root,
        )
    source = selection.source
    if ref is not None:
        if source.remote is None:
            fail("--ref is only supported with a remote source")
        if source.remote.ref != ref:
            fail(
                f"source {source.name} does not match --ref {ref!r}; existing sources are never retargeted"
            )
    depth = default_sync_depth(registry) if source.remote is not None else None
    for selector in (exact,) if exact is not None else selectors:
        validate_skill_selector(source, selector)
    registered = False
    if selection.new_entry is not None:
        if dry_run:
            emit_event(progress, f"Would register source {source.name}: {source.path}")
        else:
            add_source_entry(registry, source.name, selection.new_entry)
            write_toml(workspace_config_path(root), registry)
            registered = True
            emit_event(progress, f"registered source {source.name}: {source.path}")
        sources = {**sources, source.name: source}
    if not source.path.exists():
        if source.remote is None:
            require_source_path(source)
        try:
            sync_source(source, dry_run=dry_run, depth=depth, progress=progress)
        except (
            OSError,
            SourceSyncError,
            GitCommandError,
            subprocess.SubprocessError,
        ) as exc:
            detail = (
                format_called_process_error(exc)
                if isinstance(exc, subprocess.CalledProcessError)
                else str(exc)
            )
            raise WorkspaceError(
                f"could not obtain source {source.name}: {detail}; source registration and any checkout are retained"
            ) from exc
        if dry_run:
            emit_event(
                progress,
                "Source is not available locally; candidate list and installation plan are not yet verified",
            )
            return SkillAddReport(
                source, False, (), destination, True, provisional=True
            )
    require_source_path(source)
    if ref is not None:
        _check_requested_ref(source, ref)
    selected = _select_candidates(
        selection,
        selectors=selectors,
        all_skills=all_skills,
        exact=exact,
        sources=sources,
        ui=ui,
        dry_run=dry_run,
        progress=progress,
    )
    for candidate in selected:
        install_skill(
            destination,
            candidate.name,
            candidate.target,
            link_mode=default_link_mode(),
            dry_run=dry_run,
            progress=progress,
        )
    return SkillAddReport(
        source,
        registered,
        selected,
        destination,
        dry_run,
        provisional=dry_run and not selected,
    )
