from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .common import die, read_toml, write_toml
from .sources import Source, require_source_path

SKIP_DISCOVERY_DIRS = {
    ".agents",
    ".codex",
    ".git",
    ".hg",
    ".local",
    ".references",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "target",
}


@dataclass(frozen=True)
class SkillLinkCandidate:
    name: str
    source_name: str
    target: Path


class SkillConflictUI(Protocol):
    def is_interactive(self) -> bool: ...

    def select(
        self, name: str, candidates: tuple[SkillLinkCandidate, ...]
    ) -> Path | None: ...


def validate_profile_name(profile_name: str) -> None:
    if not profile_name or profile_name in {".", ".."} or "/" in profile_name or "\\" in profile_name:
        die(f"unsafe profile name: {profile_name}")


def profiles_root_path(repo_root: Path) -> Path:
    return repo_root / "profiles"


def profile_dir_path(repo_root: Path, profile_name: str) -> Path:
    validate_profile_name(profile_name)
    return profiles_root_path(repo_root) / profile_name


def profile_config_path(repo_root: Path, profile_name: str) -> Path:
    return profile_dir_path(repo_root, profile_name) / "config.toml"


def require_profile_schema(profile: dict) -> None:
    if "skills" in profile:
        die("legacy [[skills]] profile config is no longer supported; use [skill.<source>]")


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
    for config_path in sorted(profiles_root.glob("*/config.toml"), key=lambda path: path.parent.name):
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


def validate_profile_skill_source(source_name: str, sources: dict[str, Source]) -> None:
    if source_name == "workspace" or source_name in sources:
        return
    die(f"unknown source: {source_name}")


def iter_skill_name_matches(
    skill_name: str,
    sources: dict[str, Source],
    workspace_root: Path,
) -> list[tuple[str, Path]]:
    workspace = workspace_source(workspace_root)
    source_roots = {source.path for source in sources.values()}
    candidates = {"workspace": workspace, **sources}
    matches: list[tuple[str, Path]] = []
    for source_name, source in candidates.items():
        if not source.path.exists() or not source.path.is_dir():
            continue
        skip_roots = source_roots if source_name == "workspace" else None
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            if target.name == skill_name:
                matches.append((source_name, target))
    return matches


def split_source_selector_reference(value: str, sources: dict[str, Source]) -> tuple[str, str] | None:
    source_name, separator, selector = value.partition(":")
    if not separator or (source_name != "workspace" and source_name not in sources):
        return None
    if not selector:
        die(f"profile skill reference {value!r} is missing selector after ':'")
    return (source_name, selector)


def source_for_reference(source_name: str, sources: dict[str, Source], workspace_root: Path) -> Source:
    if source_name == "workspace":
        return workspace_source(workspace_root)
    return sources[source_name]


def source_relative_selector(source: Source, target: Path) -> str:
    try:
        return target.resolve().relative_to(source.path.resolve()).as_posix()
    except ValueError:
        return target.as_posix()


def format_reference_choices(
    matches: list[tuple[str, Path]],
    sources: dict[str, Source],
    workspace_root: Path,
    *,
    command_prefix: str | None,
    option: str | None,
) -> str:
    lines = []
    for source_name, path in matches:
        source = source_for_reference(source_name, sources, workspace_root)
        reference = f"{source_name}:{source_relative_selector(source, path)}"
        if command_prefix and option:
            lines.append(f"  {command_prefix} {option} {shlex.quote(reference)}")
        else:
            lines.append(f"  {reference}")
    return "\n".join(lines)


def resolve_profile_skill_reference(
    value: str,
    sources: dict[str, Source],
    workspace_root: Path,
    *,
    command_prefix: str | None = None,
    option: str | None = None,
) -> tuple[str, str | None]:
    source_selector = split_source_selector_reference(value, sources)
    if source_selector is not None:
        return source_selector

    if value == "workspace" or value in sources:
        return (value, None)

    matches = iter_skill_name_matches(value, sources, workspace_root)
    if not matches:
        unsynced = sorted(source.name for source in sources.values() if source.remote is not None and not source.path.exists())
        if unsynced:
            names = " ".join(unsynced)
            die(f"unknown source or skill: {value}; unsynced sources may contain it, run: hgc source sync {names}")
        die(f"unknown source or skill: {value}")
    if len(matches) > 1:
        choices = format_reference_choices(
            matches,
            sources,
            workspace_root,
            command_prefix=command_prefix,
            option=option,
        )
        die(f"skill name {value!r} is ambiguous. Choose one:\n{choices}")
    source_name, _path = matches[0]
    return (source_name, value)


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
        die(f"profile skill {source_name} field {field} must be a string list")
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
            include_values = require_string_list(current_include, field="include", source_name=source_name)
            if "*" not in include_values:
                config["include"] = dedupe_append(include_values, include)
    if exclude:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(current_exclude, field="exclude", source_name=source_name)
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, exclude)
    skills[source_name] = config


def remove_profile_skill(profile: dict, source_name: str) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        die(f"profile does not reference skill source: {source_name}")
    del skills[source_name]
    if not skills:
        profile.pop("skill", None)


def remove_profile_skill_selector(profile: dict, source_name: str, selector: str) -> None:
    require_profile_schema(profile)
    skills = profile.get("skill", {})
    if source_name not in skills:
        die(f"profile does not reference skill source: {source_name}")

    config = dict(skills.get(source_name) or {})
    current_include = config.get("include")
    if current_include is None:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(current_exclude, field="exclude", source_name=source_name)
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, [selector])
        skills[source_name] = config
        return

    include_values = require_string_list(current_include, field="include", source_name=source_name)
    if "*" in include_values:
        current_exclude = config.get("exclude")
        exclude_values = (
            require_string_list(current_exclude, field="exclude", source_name=source_name)
            if current_exclude is not None
            else []
        )
        config["exclude"] = dedupe_append(exclude_values, [selector])
        skills[source_name] = config
        return

    remaining = [item for item in include_values if item != selector]
    if len(remaining) == len(include_values):
        die(f"profile skill source {source_name} does not include skill: {selector}")
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
        validate_profile_skill_source(add_skill, sources)
        set_profile_skill(profile, add_skill, include=include, exclude=exclude, replace=True)
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
        updated["skill"] = {name: dict(config or {}) for name, config in profile.get("skill", {}).items()}

    changed = False
    if description is not None:
        updated["description"] = description
        changed = True
    if add_skill:
        validate_profile_skill_source(add_skill, sources)
        set_profile_skill(updated, add_skill, include=include, exclude=exclude, replace=replace)
        changed = True
    if remove_skill:
        remove_profile_skill(updated, remove_skill)
        changed = True
    if remove_skill_selector:
        remove_profile_skill_selector(updated, remove_skill_selector[0], remove_skill_selector[1])
        changed = True
    if not changed:
        die("profile update requires at least one change")
    return updated


def remove_profile_directory(repo_root: Path, profile_name: str) -> Path:
    profile_dir = profile_dir_path(repo_root, profile_name)
    if not profile_dir.exists():
        die(f"unknown profile: {profile_name}")
    shutil.rmtree(profile_dir)
    return profile_dir


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def discover_skill_dirs(root: Path, *, skip_roots: set[Path] | None = None) -> list[Path]:
    matches: list[Path] = []
    resolved_skips = {path.resolve() for path in (skip_roots or set())}
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current).resolve()
        if any(is_relative_to(current_path, skip_root) for skip_root in resolved_skips):
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name not in SKIP_DISCOVERY_DIRS]
        dirnames[:] = [
            name
            for name in dirnames
            if not any(is_relative_to((Path(current) / name).resolve(), skip_root) for skip_root in resolved_skips)
        ]
        if "SKILL.md" not in filenames:
            continue
        matches.append(Path(current))
    return sorted(matches, key=lambda path: str(path))


def discover_skill_links(
    source: Source,
    *,
    prefix: str | None = None,
    skip_roots: set[Path] | None = None,
) -> list[tuple[str, Path]]:
    root = source.path
    if prefix:
        root = root / prefix
        if not root.exists():
            die(f"skill prefix for source {source.name} does not exist: {root}")
        if (root / "SKILL.md").exists():
            matches = [root]
        else:
            matches = discover_skill_dirs(root, skip_roots=skip_roots)
    else:
        matches = discover_skill_dirs(root, skip_roots=skip_roots)

    if not matches:
        prefix_text = f" under prefix {prefix!r}" if prefix else ""
        die(f"no SKILL.md files found in source {source.name}{prefix_text}: {source.path}")

    return [(target.name, target) for target in matches]


def require_unique_link_names(links: list[tuple[str, Path]]) -> None:
    seen: dict[str, Path] = {}
    for name, target in links:
        existing = seen.get(name)
        if existing is not None:
            die(
                f"duplicate discovered skill name {name!r}: {existing} and {target}; "
                "use include with a more specific path prefix"
            )
        seen[name] = target


def format_selector_choices(source: Source, matches: list[tuple[str, Path]]) -> str:
    return ", ".join(source_relative_selector(source, path) for _name, path in matches)


def workspace_source(workspace_root: Path) -> Source:
    return Source(name="workspace", path=workspace_root, remote=None)


def resolve_selector(
    source: Source,
    selector: str,
    *,
    skip_roots: set[Path] | None = None,
    allow_name_conflicts: bool = False,
) -> list[tuple[str, Path]]:
    if selector == "*":
        links = discover_skill_links(source, skip_roots=skip_roots)
        if not allow_name_conflicts:
            require_unique_link_names(links)
        return links

    prefix_root = source.path / selector
    if prefix_root.exists():
        matches = discover_skill_links(source, prefix=selector, skip_roots=skip_roots)
    else:
        matches = [(name, path) for name, path in discover_skill_links(source, skip_roots=skip_roots) if name == selector]
        if not matches:
            die(f"skill selector {selector!r} for source {source.name} matched no candidates")

    if len(matches) > 1 and not allow_name_conflicts:
        die(
            f"skill selector {selector!r} for source {source.name} matched multiple candidates. "
            f"Use a more specific selector: {format_selector_choices(source, matches)}"
        )
    return matches


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
    if not source.path.exists() or not source.path.is_dir():
        return

    skip_roots = {source.path for source in sources.values()} if source_name == "workspace" else None
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


def resolve_link_name_conflicts(
    links: list[SkillLinkCandidate],
    conflict_ui: SkillConflictUI | None,
    *,
    preview_conflicts: bool = False,
) -> list[SkillLinkCandidate]:
    candidates_by_name: dict[str, list[SkillLinkCandidate]] = {}
    resolved_by_name: dict[str, set[Path]] = {}
    for link in links:
        resolved = link.target.resolve()
        if resolved in resolved_by_name.setdefault(link.name, set()):
            continue
        resolved_by_name[link.name].add(resolved)
        candidates_by_name.setdefault(link.name, []).append(link)

    selected: list[SkillLinkCandidate] = []
    for name, candidates in candidates_by_name.items():
        if len(candidates) == 1:
            selected.append(candidates[0])
            continue

        if conflict_ui is None or not conflict_ui.is_interactive():
            if preview_conflicts:
                print(f"conflict {name!r}: choose one source when running interactively")
                for candidate in candidates:
                    print(f"  candidate {candidate.source_name}: {candidate.target}")
                continue
            choices = " and ".join(
                f"{candidate.source_name}: {candidate.target}"
                for candidate in candidates
            )
            die(
                f"duplicate discovered skill name {name!r}: {choices}; "
                "rerun in an interactive terminal to choose one or narrow the "
                "profile's include selector"
            )

        try:
            choice = conflict_ui.select(name, tuple(candidates))
        except (OSError, RuntimeError) as exc:
            die(
                f"interactive skill source selection failed for {name!r}: {exc}; "
                "rerun in a working terminal or narrow the profile's include selector"
            )
        if choice is None:
            die(f"skill source selection cancelled for {name!r}")
        matching = next(
            (
                candidate
                for candidate in candidates
                if candidate.target.resolve() == choice.resolve()
            ),
            None,
        )
        if matching is None:
            die(f"invalid skill source selected for {name!r}: {choice}")
        selected.append(matching)

    return selected


def is_windows_platform() -> bool:
    return os.name == "nt"


def default_profile_link_mode() -> str:
    return "junction" if is_windows_platform() else "symlink"


def is_junction(path: Path) -> bool:
    return hasattr(path, "is_junction") and path.is_junction()


def symlink_failure_message(link: Path, target: Path, error: OSError) -> str:
    message = f"could not create symlink {link} -> {target}: {error}"
    if is_windows_platform():
        message += "; on Windows, rerun PowerShell or Git Bash as Administrator, use --link-mode junction, or use -cp"
    return message


def junction_failure_message(link: Path, target: Path, error: BaseException) -> str:
    return f"could not create junction {link} -> {target}: {error}"


def create_windows_junction(link: Path, target: Path) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HAGENCY_PROFILE_JUNCTION_LINK": str(link),
            "HAGENCY_PROFILE_JUNCTION_TARGET": str(target),
        }
    )
    command = (
        "New-Item -ItemType Junction "
        "-Path $env:HAGENCY_PROFILE_JUNCTION_LINK "
        "-Target $env:HAGENCY_PROFILE_JUNCTION_TARGET | Out-Null"
    )
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=True,
        text=True,
        env=env,
    )


def _validate_skills_dir(skills_dir: Path) -> None:
    for candidate in (skills_dir, *skills_dir.parents):
        if candidate.is_symlink() and not candidate.exists():
            die(f"skills destination is a broken symlink: {candidate}")
        if candidate.exists():
            if not candidate.is_dir():
                die(f"skills destination is not a directory: {candidate}")
            return


def _create_skills_dir(skills_dir: Path) -> None:
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        die(f"could not create skills destination {skills_dir}: {exc}")


def install_skill(skills_dir: Path, name: str, target: Path, *, link_mode: str, dry_run: bool) -> None:
    _validate_skills_dir(skills_dir)
    if not target.exists() and not dry_run:
        die(f"link target does not exist: {target}")
    real_target = target.resolve() if target.exists() else target.absolute()
    link = skills_dir / name

    if link_mode == "copy":
        if link.is_symlink() or link.exists():
            die(f"refusing to overwrite existing skill destination: {link}")
        print(f"copy {real_target} -> {link}")
        if not dry_run:
            if not real_target.is_dir():
                die(f"copy target is not a directory: {real_target}")
            _create_skills_dir(skills_dir)
            shutil.copytree(real_target, link, symlinks=False)
        return

    if link_mode == "junction":
        if not is_windows_platform():
            die("profile init link mode junction is only supported on Windows")
        if not dry_run and not real_target.is_dir():
            die(f"junction target is not a directory: {real_target}")
        if is_junction(link):
            existing = link.resolve()
            if existing == real_target:
                print(f"ok {link} -> {real_target}")
                return
            print(f"remove {link}")
            if not dry_run:
                link.rmdir()
        elif link.is_symlink():
            print(f"remove {link}")
            if not dry_run:
                link.unlink()
        elif link.exists():
            die(f"refusing to overwrite non-junction: {link}")

        print(f"junction {link} -> {real_target}")
        if not dry_run:
            _create_skills_dir(skills_dir)
            try:
                create_windows_junction(link, real_target)
            except (OSError, subprocess.CalledProcessError) as exc:
                die(junction_failure_message(link, real_target, exc))
        return

    if link_mode != "symlink":
        die(f"unsupported profile init link mode: {link_mode}")

    if link.is_symlink():
        existing = link.resolve()
        if existing == real_target:
            print(f"ok {link} -> {real_target}")
            return
        print(f"remove {link}")
        if not dry_run:
            link.unlink()
    elif link.exists():
        die(f"refusing to overwrite non-symlink: {link}")

    print(f"link {link} -> {real_target}")
    if not dry_run:
        _create_skills_dir(skills_dir)
        try:
            os.symlink(real_target, link, target_is_directory=real_target.is_dir())
        except OSError as exc:
            die(symlink_failure_message(link, real_target, exc))


def skill_source(source_name: str, sources: dict[str, Source], workspace: Source) -> Source:
    if source_name == "workspace":
        return workspace
    source = sources.get(source_name)
    if source is None:
        die(f"profile references unknown source: {source_name}")
    return source


def init_profile(
    profile: dict,
    sources: dict[str, Source],
    workspace_root: Path,
    skills_dir: Path,
    *,
    link_mode: str,
    dry_run: bool,
    conflict_ui: SkillConflictUI | None = None,
) -> None:
    _validate_skills_dir(skills_dir)
    workspace = workspace_source(workspace_root)
    if "skills" in profile:
        die("legacy [[skills]] profile config is no longer supported; use [skill.<source>]")

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

    for link in resolve_link_name_conflicts(
        links,
        conflict_ui,
        preview_conflicts=dry_run,
    ):
        install_skill(
            skills_dir,
            link.name,
            link.target,
            link_mode=link_mode,
            dry_run=dry_run,
        )
