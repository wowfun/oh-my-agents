from __future__ import annotations

import shutil
from pathlib import Path

from hagency_cli.workspace.config import read_toml, write_toml
from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress
from hagency_cli.workspace.skills import (
    SkillConflictUI,
    SkillLinkCandidate,
    validate_skills_dir,
    install_skill,
    require_unique_link_names,
    resolve_link_name_conflicts,
    resolve_selector,
    skill_source,
    validate_skill_selector,
    validate_skill_source,
    workspace_source,
)
from hagency_cli.workspace.sources import Source, require_source_path


def find_profile_source_references(repo_root: Path, source_name: str) -> list[Path]:
    profiles_root = repo_root / "profiles"
    if not profiles_root.exists():
        return []

    references: list[Path] = []
    for profile_config in sorted(profiles_root.glob("*/config.toml")):
        profile = read_toml(profile_config)
        if "skills" in profile:
            fail(
                "legacy [[skills]] profile config is no longer supported; use [skill.<source>]"
            )
        if source_name in profile.get("skill", {}):
            references.append(profile_config)
    return references


def validate_profile_name(profile_name: str) -> None:
    if (
        not profile_name
        or profile_name in {".", ".."}
        or "/" in profile_name
        or "\\" in profile_name
    ):
        fail(f"unsafe profile name: {profile_name}")


def profiles_root_path(repo_root: Path) -> Path:
    return repo_root / "profiles"


def profile_dir_path(repo_root: Path, profile_name: str) -> Path:
    validate_profile_name(profile_name)
    return profiles_root_path(repo_root) / profile_name


def profile_config_path(repo_root: Path, profile_name: str) -> Path:
    return profile_dir_path(repo_root, profile_name) / "config.toml"


def require_profile_schema(profile: dict) -> None:
    if "skills" in profile:
        fail(
            "legacy [[skills]] profile config is no longer supported; use [skill.<source>]"
        )


def read_profile_config(repo_root: Path, profile_name: str) -> dict:
    profile = read_toml(profile_config_path(repo_root, profile_name))
    require_profile_schema(profile)
    return profile


def write_profile_config(repo_root: Path, profile_name: str, profile: dict) -> None:
    path = profile_config_path(repo_root, profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_toml(path, profile)


def list_profile_configs(repo_root: Path) -> list[tuple[str, dict]]:
    profiles_root = profiles_root_path(repo_root)
    if not profiles_root.exists():
        return []

    profiles = []
    for config_path in sorted(
        profiles_root.glob("*/config.toml"), key=lambda path: path.parent.name
    ):
        profile = read_toml(config_path)
        require_profile_schema(profile)
        profiles.append((config_path.parent.name, profile))
    return profiles


def profile_source_names(profile: dict) -> list[str]:
    names = []
    require_profile_schema(profile)
    for source_name in profile.get("skill", {}):
        if source_name != "workspace":
            names.append(source_name)
    return names


def profile_skill_names(profile: dict) -> list[str]:
    require_profile_schema(profile)
    return list(profile.get("skill", {}))


def dedupe_append(existing: list[str], additions: list[str]) -> list[str]:
    values = list(existing)
    seen = set(values)
    for item in additions:
        if item not in seen:
            values.append(item)
            seen.add(item)
    return values


def require_string_list(value: object, *, field: str, source_name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        fail(f"profile skill {source_name} field {field} must be a string list")
    return value


def set_profile_skill(
    profile: dict,
    source_name: str,
    *,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
) -> None:
    require_profile_schema(profile)
    skills = profile.setdefault("skill", {})
    existing = skills.get(source_name)

    if replace or existing is None:
        config: dict[str, list[str]] = {}
        if include:
            config["include"] = dedupe_append([], include)
        if exclude:
            config["exclude"] = dedupe_append([], exclude)
        skills[source_name] = config
        return

    config = dict(existing or {})
    if include:
        current_include = config.get("include")
        if current_include is not None:
            include_values = require_string_list(
                current_include, field="include", source_name=source_name
            )
            if "*" not in include_values:
                config["include"] = dedupe_append(include_values, include)
    if exclude:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(
                current_exclude, field="exclude", source_name=source_name
            )
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, exclude)
    skills[source_name] = config


def remove_profile_skill(profile: dict, source_name: str) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        fail(f"profile does not reference skill source: {source_name}")
    del skills[source_name]
    if not skills:
        profile.pop("skill", None)


def remove_profile_skill_selector(
    profile: dict, source_name: str, selector: str
) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        fail(f"profile does not reference skill source: {source_name}")

    config = dict(skills.get(source_name) or {})
    current_include = config.get("include")
    if current_include is None:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(
                current_exclude, field="exclude", source_name=source_name
            )
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, [selector])
        skills[source_name] = config
        return

    include_values = require_string_list(
        current_include, field="include", source_name=source_name
    )
    if "*" in include_values:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(
                current_exclude, field="exclude", source_name=source_name
            )
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, [selector])
        skills[source_name] = config
        return

    remaining = [item for item in include_values if item != selector]
    if len(remaining) == len(include_values):
        fail(f"profile skill source {source_name} does not include skill: {selector}")
    if remaining:
        config["include"] = remaining
        skills[source_name] = config
        return

    del skills[source_name]
    if not skills:
        profile.pop("skill", None)


def build_profile_config(
    profile_name: str,
    *,
    description: str | None,
    add_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    sources: dict[str, Source],
) -> dict:
    profile = {"name": profile_name}
    if description is not None:
        profile["description"] = description
    if add_skill:
        validate_skill_source(add_skill, sources)
        set_profile_skill(
            profile, add_skill, include=include, exclude=exclude, replace=True
        )
    return profile


def update_profile_config(
    profile: dict,
    *,
    description: str | None,
    add_skill: str | None,
    remove_skill: str | None,
    remove_skill_selector: tuple[str, str] | None,
    include: list[str] | None,
    exclude: list[str] | None,
    replace: bool,
    sources: dict[str, Source],
) -> dict:
    require_profile_schema(profile)
    updated = dict(profile)
    if "skill" in profile:
        updated["skill"] = {
            name: dict(config or {})
            for name, config in profile.get("skill", {}).items()
        }

    changed = False
    if description is not None:
        updated["description"] = description
        changed = True
    if add_skill:
        validate_skill_source(add_skill, sources)
        set_profile_skill(
            updated, add_skill, include=include, exclude=exclude, replace=replace
        )
        changed = True
    if remove_skill:
        remove_profile_skill(updated, remove_skill)
        changed = True
    if remove_skill_selector:
        remove_profile_skill_selector(
            updated, remove_skill_selector[0], remove_skill_selector[1]
        )
        changed = True
    if not changed:
        fail("profile update requires at least one change")
    return updated


def remove_profile_directory(repo_root: Path, profile_name: str) -> Path:
    profile_dir = profile_dir_path(repo_root, profile_name)
    if not profile_dir.exists():
        fail(f"unknown profile: {profile_name}")
    shutil.rmtree(profile_dir)
    return profile_dir


def validate_profile_skill_selectors(
    source_name: str,
    sources: dict[str, Source],
    workspace_root: Path,
    *,
    include: list[str] | None,
    exclude: list[str] | None,
) -> None:
    selectors = [*(include or []), *(exclude or [])]
    if not selectors:
        return

    workspace = workspace_source(workspace_root)
    source = skill_source(source_name, sources, workspace)
    for selector in selectors:
        validate_skill_selector(source, selector)
    if not source.path.exists() or not source.path.is_dir():
        return

    skip_roots = (
        {source.path for source in sources.values()}
        if source_name == "workspace"
        else None
    )
    for selector in selectors:
        resolve_selector(source, selector, skip_roots=skip_roots)


def selected_links(
    config: dict,
    source: Source,
    *,
    skip_roots: set[Path] | None = None,
    allow_name_conflicts: bool = False,
) -> list[tuple[str, Path]]:
    includes = config.get("include") or ["*"]
    excludes = set(config.get("exclude") or [])

    links: list[tuple[str, Path]] = []
    for item in includes:
        links.extend(
            resolve_selector(
                source,
                item,
                skip_roots=skip_roots,
                allow_name_conflicts=allow_name_conflicts,
            )
        )

    excluded_paths: set[Path] = set()
    for item in excludes:
        for _name, target in resolve_selector(
            source,
            item,
            skip_roots=skip_roots,
            allow_name_conflicts=allow_name_conflicts,
        ):
            excluded_paths.add(target.resolve())

    filtered = [
        (name, target)
        for name, target in links
        if target.resolve() not in excluded_paths
    ]
    if not allow_name_conflicts:
        require_unique_link_names(filtered)
    return filtered


def apply_profile(
    profile: dict,
    sources: dict[str, Source],
    workspace_root: Path,
    skills_dir: Path,
    *,
    link_mode: str,
    dry_run: bool,
    conflict_ui: SkillConflictUI | None = None,
    progress: Progress | None = None,
) -> tuple[SkillLinkCandidate, ...]:
    validate_skills_dir(skills_dir)
    workspace = workspace_source(workspace_root)
    if "skills" in profile:
        fail(
            "legacy [[skills]] profile config is no longer supported; use [skill.<source>]"
        )

    source_roots = {source.path for source in sources.values()}
    links: list[SkillLinkCandidate] = []
    for source_name, config in profile.get("skill", {}).items():
        source = skill_source(source_name, sources, workspace)
        require_source_path(source)
        skip_roots = source_roots if source.name == "workspace" else None
        links.extend(
            SkillLinkCandidate(
                name=link_name,
                source_name=source_name,
                target=target,
            )
            for link_name, target in selected_links(
                config or {},
                source,
                skip_roots=skip_roots,
                allow_name_conflicts=True,
            )
        )

    selected = resolve_link_name_conflicts(
        links, conflict_ui, preview_conflicts=dry_run, progress=progress
    )
    for link in selected:
        install_skill(
            skills_dir,
            link.name,
            link.target,
            link_mode=link_mode,
            dry_run=dry_run,
            progress=progress,
        )
    return tuple(selected)
