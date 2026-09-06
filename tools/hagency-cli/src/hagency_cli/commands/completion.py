from __future__ import annotations

import os
import shlex
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import typer

from hagency_cli.paths import expand_path
from hagency_cli.workspace.catalog import SkillCatalogEntry, discover_catalog
from hagency_cli.workspace.config import read_toml
from hagency_cli.workspace.discovery import (
    resolve_workspace_root,
    workspace_config_path,
)
from hagency_cli.workspace.source_inputs import (
    classify_skill_input,
    is_explicit_path,
    remote_identity,
)
from hagency_cli.workspace.sources import Source, resolve_sources


@dataclass(frozen=True)
class CompletionCatalog:
    root: Path
    sources: dict[str, Source]
    profiles: tuple[str, ...]
    skills: tuple[SkillCatalogEntry, ...]


def _raw_option_value(*names: str) -> str | None:
    raw_words = os.environ.get("COMP_WORDS")
    if not raw_words:
        return None
    try:
        words = shlex.split(raw_words)
    except ValueError:
        words = raw_words.split()
    value = None
    for index, word in enumerate(words[:-1]):
        if word in names:
            value = words[index + 1]
    return value


def _context_value(ctx: typer.Context, key: str, *option_names: str) -> str | None:
    value = ctx.params.get(key)
    if isinstance(value, str) and value:
        return value
    return _raw_option_value(*option_names)


def _quiet_catalog(ctx: typer.Context) -> CompletionCatalog | None:
    root_value = _context_value(ctx, "root", "--root", "-r")
    checkout_dir = _context_value(ctx, "checkout_dir", "--checkout-dir")
    try:
        root = resolve_workspace_root(root_value, Path.cwd())
        registry = read_toml(workspace_config_path(root))
        sources = resolve_sources(
            registry, repo_root=root, checkout_override=checkout_dir
        )
        profiles = _profile_names(root)
        skills = _skill_candidates(root, sources)
    except Exception:
        # Completion is best-effort and must stay silent for malformed or
        # inaccessible workspace data.
        return None
    return CompletionCatalog(
        root=root, sources=sources, profiles=profiles, skills=skills
    )


def _profile_names(root: Path) -> tuple[str, ...]:
    profiles_root = root / "profiles"
    if not profiles_root.is_dir():
        return ()
    return tuple(
        path.parent.name
        for path in sorted(
            profiles_root.glob("*/config.toml"), key=lambda item: item.parent.name
        )
        if path.is_file()
    )


def _skill_candidates(
    root: Path, sources: dict[str, Source]
) -> tuple[SkillCatalogEntry, ...]:
    return discover_catalog(root, sources)


def _used_values(ctx: typer.Context) -> set[str]:
    used: set[str] = set()
    for value in ctx.params.values():
        if isinstance(value, str):
            used.add(value)
        elif isinstance(value, list | tuple):
            used.update(item for item in value if isinstance(item, str))
    return used


def _items(
    values: Iterable[tuple[str, str]],
    incomplete: str,
    *,
    excluded: set[str] | None = None,
) -> list[tuple[str, str]]:
    excluded = excluded or set()
    deduped: dict[str, str] = {}
    for value, help_text in values:
        if value in excluded or not value.startswith(incomplete):
            continue
        deduped.setdefault(value, help_text)
    return [(value, deduped[value]) for value in sorted(deduped)]


def _reference_values(
    catalog: CompletionCatalog,
    *,
    source_names: set[str] | None = None,
    include_sources: bool,
) -> list[tuple[str, str]]:
    skills = [
        skill
        for skill in catalog.skills
        if source_names is None or skill.source_name in source_names
    ]
    name_counts = Counter(skill.name for skill in skills)
    values: list[tuple[str, str]] = []
    if include_sources:
        names = (
            source_names
            if source_names is not None
            else {"workspace", *catalog.sources}
        )
        values.extend((name, "source") for name in names)
    values.extend(
        (skill.name, f"skill from {skill.source_name}")
        for skill in skills
        if name_counts[skill.name] == 1
    )
    values.extend((skill.reference, "exact skill selector") for skill in skills)
    return values


def complete_source(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    return _items(
        ((name, "source") for name in catalog.sources),
        incomplete,
        excluded=_used_values(ctx),
    )


def complete_source_or_workspace(
    ctx: typer.Context, incomplete: str
) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    return _items(
        ((name, "source") for name in ("workspace", *catalog.sources)),
        incomplete,
        excluded=_used_values(ctx),
    )


def complete_profile(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    return _items(((name, "profile") for name in catalog.profiles), incomplete)


def complete_skill_add(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    if is_explicit_path(incomplete):
        return complete_directory(incomplete)
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    return _items(_reference_values(catalog, include_sources=True), incomplete)


def complete_skill_reference(
    ctx: typer.Context, incomplete: str
) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    return _items(_reference_values(catalog, include_sources=True), incomplete)


def complete_profile_remove_reference(
    ctx: typer.Context, incomplete: str
) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    profile_name = ctx.params.get("name") or _raw_profile_name()
    if catalog is None or not isinstance(profile_name, str):
        return []
    try:
        profile = read_toml(catalog.root / "profiles" / profile_name / "config.toml")
        source_names = set(profile.get("skill", {}))
    except Exception:
        return []
    return _items(
        _reference_values(catalog, source_names=source_names, include_sources=True),
        incomplete,
    )


def _raw_profile_name() -> str | None:
    raw_words = os.environ.get("COMP_WORDS")
    if not raw_words:
        return None
    try:
        words = shlex.split(raw_words)
    except ValueError:
        words = raw_words.split()
    for group in ("profile", "p"):
        for command in ("update", "u"):
            try:
                index = words.index(group)
            except ValueError:
                continue
            if len(words) > index + 2 and words[index + 1] == command:
                return words[index + 2]
    return None


def complete_selector(ctx: typer.Context, incomplete: str) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    if catalog is None:
        return []
    reference = ctx.params.get("add_skill") or _raw_option_value("-AS", "--add-skill")
    if not isinstance(reference, str):
        return []
    source_name = _reference_source(catalog, reference)
    if source_name is None:
        return []
    values = [("*", "all skills")]
    values.extend(
        (skill.selector, f"skill {skill.name}")
        for skill in catalog.skills
        if skill.source_name == source_name
    )
    return _items(values, incomplete, excluded=_used_values(ctx))


def _reference_source(catalog: CompletionCatalog, reference: str) -> str | None:
    source_names = {"workspace", *catalog.sources}
    if reference in source_names:
        return reference
    source_name, separator, _selector = reference.partition(":")
    if separator and source_name in source_names:
        return source_name
    matches = {skill.source_name for skill in catalog.skills if skill.name == reference}
    if len(matches) == 1:
        return next(iter(matches))
    return None


def complete_directory(incomplete: str) -> list[tuple[str, str]]:
    return _complete_path(incomplete, directories_only=True)


def complete_file(incomplete: str) -> list[tuple[str, str]]:
    return _complete_path(incomplete, directories_only=False)


def _complete_path(incomplete: str, *, directories_only: bool) -> list[tuple[str, str]]:
    raw = incomplete or "./"
    expanded = expand_path(raw, Path.cwd())
    directory = expanded if raw.endswith(("/", "\\")) else expanded.parent
    name_prefix = "" if raw.endswith(("/", "\\")) else expanded.name
    try:
        children = sorted(
            (
                child
                for child in directory.iterdir()
                if (not directories_only or child.is_dir())
                and child.name.startswith(name_prefix)
            ),
            key=lambda child: child.name,
        )
    except OSError:
        return []

    raw_parent = raw if raw.endswith(("/", "\\")) else str(Path(raw).parent)
    if raw_parent == ".":
        raw_parent = ""
    return [
        (
            str(Path(raw_parent) / child.name) + (os.sep if child.is_dir() else ""),
            "directory" if child.is_dir() else "file",
        )
        for child in children
    ]


def complete_install_selector(
    ctx: typer.Context, incomplete: str
) -> list[tuple[str, str]]:
    catalog = _quiet_catalog(ctx)
    reference = ctx.params.get("skill") or _raw_skill_input()
    if catalog is None or not isinstance(reference, str):
        return []
    try:
        parsed = classify_skill_input(reference, catalog.sources)
        if parsed.kind == "source":
            skills = [s for s in catalog.skills if s.source_name == parsed.value]
        elif parsed.kind == "path":
            path = expand_path(parsed.value, Path.cwd()).resolve()
            skills = [s for s in catalog.skills if s.path.is_relative_to(path)]
        elif parsed.kind == "url":
            names = {
                s.name
                for s in catalog.sources.values()
                if s.remote
                and remote_identity(s.remote.url) == remote_identity(parsed.value)
            }
            skills = [s for s in catalog.skills if s.source_name in names]
        else:
            return []
    except Exception:
        return []
    return _items(
        ((s.selector, s.name) for s in skills), incomplete, excluded=_used_values(ctx)
    )


def _raw_skill_input() -> str | None:
    """Click may leave positional arguments unset during option completion."""
    try:
        words = shlex.split(os.environ.get("COMP_WORDS", ""))
        start = words.index("skill")
    except ValueError:
        return None
    if words[start + 1 : start + 2] != ["add"]:
        return None
    value_options = {
        "--root",
        "-r",
        "--path",
        "-p",
        "--dir",
        "-d",
        "--checkout-dir",
        "--skill",
        "-s",
        "--source-name",
        "--ref",
    }
    index = start + 2
    while index < len(words):
        word = words[index]
        if word == "--":
            return words[index + 1] if index + 1 < len(words) else None
        if word in value_options:
            index += 2
        elif word.startswith("-"):
            index += 1
        else:
            return word
    return None
