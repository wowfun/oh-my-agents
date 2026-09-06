from __future__ import annotations

from pathlib import Path

from hagency_cli.workspace.config import render_toml
from hagency_cli.workspace.context import load_sources, workspace_root_arg
from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.profiles import (
    apply_profile,
    build_profile_config,
    profile_config_path,
    profile_dir_path,
    read_profile_config,
    remove_profile_directory,
    update_profile_config,
    validate_profile_name,
    validate_profile_skill_selectors,
    write_profile_config,
)
from hagency_cli.workspace.skills import (
    LinkMode,
    SkillConflictUI,
    SkillLinkCandidate,
    resolve_link_mode,
    resolve_skill_install_dir,
    resolve_skill_reference,
)


def apply_profile_to_directory(
    *,
    name: str,
    skills_path: str | None,
    skills_root: str | None,
    copy: bool,
    link_mode: LinkMode | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    conflict_ui: SkillConflictUI | None = None,
    progress: Progress | None = None,
) -> tuple[SkillLinkCandidate, ...]:

    invocation_cwd = Path.cwd()
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)
    profile = read_profile_config(root, name)
    skills_dir = resolve_skill_install_dir(
        skills_path,
        skills_root,
        False,
        invocation_cwd,
        default_root=None,
    )
    return apply_profile(
        profile,
        sources,
        root,
        skills_dir,
        link_mode=resolve_link_mode(copy, link_mode),
        dry_run=dry_run,
        conflict_ui=conflict_ui,
        progress=progress,
    )


def validate_profile_skill_args(
    include: list[str] | None,
    exclude: list[str] | None,
    add_skill: str | None,
) -> tuple[list[str] | None, list[str] | None]:
    if (include or exclude) and not add_skill:
        fail("--include and --exclude require --add-skill")
    return include, exclude


def with_inferred_include(
    include: list[str] | None, selector: str | None
) -> list[str] | None:
    if selector is None:
        return include
    values = [selector]
    for item in include or []:
        if item not in values:
            values.append(item)
    return values


def add_profile(
    *,
    name: str,
    description: str | None,
    add_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> dict:
    root = workspace_root_arg(root_value)
    validate_profile_name(name)
    profile_dir = profile_dir_path(root, name)
    if profile_dir.exists():
        fail(f"profile already exists: {name}")
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    sources = load_sources(root, checkout_dir) if add_skill else {}
    if add_skill:
        add_skill, inferred_include = resolve_skill_reference(
            add_skill,
            sources,
            root,
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(
            add_skill, sources, root, include=include, exclude=exclude
        )
    profile = build_profile_config(
        name,
        description=description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        sources=sources,
    )

    if dry_run:
        emit_event(progress, f"Would create profile: {profile_config_path(root, name)}")
        emit_event(progress, render_toml(profile).rstrip())
        return profile

    write_profile_config(root, name, profile)
    emit_event(progress, f"added profile: {name}")
    return profile


def update_profile(
    *,
    name: str,
    description: str | None,
    add_skill: str | None,
    remove_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> dict:
    root = workspace_root_arg(root_value)
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    if replace and not add_skill:
        fail("--replace requires --add-skill")
    profile = read_profile_config(root, name)
    sources = load_sources(root, checkout_dir) if add_skill or remove_skill else {}
    if add_skill:
        add_skill, inferred_include = resolve_skill_reference(
            add_skill,
            sources,
            root,
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(
            add_skill, sources, root, include=include, exclude=exclude
        )
    remove_skill_selector = None
    if remove_skill:
        remove_skill, inferred_remove = resolve_skill_reference(
            remove_skill,
            sources,
            root,
        )
        if inferred_remove is not None:
            validate_profile_skill_selectors(
                remove_skill, sources, root, include=[inferred_remove], exclude=None
            )
            remove_skill_selector = (remove_skill, inferred_remove)
            remove_skill = None
    updated = update_profile_config(
        profile,
        description=description,
        add_skill=add_skill,
        remove_skill=remove_skill,
        remove_skill_selector=remove_skill_selector,
        include=include,
        exclude=exclude,
        replace=replace,
        sources=sources,
    )

    if dry_run:
        emit_event(progress, f"Would update profile: {profile_config_path(root, name)}")
        emit_event(progress, render_toml(updated).rstrip())
        return updated

    write_profile_config(root, name, updated)
    emit_event(progress, f"updated profile: {name}")
    return updated


def remove_profile(
    *,
    name: str,
    root_value: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> Path:
    root = workspace_root_arg(root_value)
    profile_dir = profile_dir_path(root, name)
    if not profile_dir.exists():
        fail(f"unknown profile: {name}")

    if dry_run:
        emit_event(progress, f"Would remove profile directory: {profile_dir}")
        return profile_dir

    remove_profile_directory(root, name)
    emit_event(progress, f"removed profile: {name}")
    return profile_dir
