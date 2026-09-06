from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.skills import (
    discover_skill_dirs,
    discover_skill_links,
    resolve_selector,
    skill_skip_roots,
    skill_source,
    source_relative_selector,
    workspace_source,
)
from hagency_cli.workspace.sources import Source, require_source_path


@dataclass(frozen=True)
class SkillCatalogEntry:
    source_name: str
    name: str
    selector: str
    path: Path

    @property
    def reference(self) -> str:
        return f"{self.source_name}:{self.selector}"


def dedupe_preserve_order(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def catalog_entry(
    source_name: str, source: Source, name: str, target: Path
) -> SkillCatalogEntry:
    selector = source_relative_selector(source, target)
    return SkillCatalogEntry(source_name, name, selector, target.resolve())


def list_all_skill_rows(
    root: Path, sources: dict[str, Source], *, progress: Progress | None = None
) -> list[SkillCatalogEntry]:
    rows = []
    candidates = [("workspace", workspace_source(root)), *sources.items()]
    for source_name, source in candidates:
        if not source.path.exists():
            emit_event(
                progress,
                f"Warning: skipping missing source {source_name}: {source.path}",
                error=True,
            )
            continue
        if not source.path.is_dir():
            emit_event(
                progress,
                f"Warning: skipping non-directory source {source_name}: {source.path}",
                error=True,
            )
            continue
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(catalog_entry(source_name, source, target.name, target))
    return rows


def validate_skill_source_filters(
    source_filters: list[str], sources: dict[str, Source], root: Path
) -> list[str]:
    selected = dedupe_preserve_order(source_filters)
    available = {"workspace": workspace_source(root), **sources}
    for source_name in selected:
        source = available.get(source_name)
        if source is None:
            fail(f"unknown source: {source_name}")
        require_source_path(source)
    return selected


def list_filtered_skill_rows(
    root: Path, sources: dict[str, Source], source_filters: list[str]
) -> list[SkillCatalogEntry]:
    rows = []
    available = {"workspace": workspace_source(root), **sources}
    for source_name in validate_skill_source_filters(source_filters, sources, root):
        source = available[source_name]
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(catalog_entry(source_name, source, target.name, target))
    return rows


def list_selector_links(
    source: Source, selector: str, *, skip_roots: set[Path] | None = None
) -> list[tuple[str, Path]]:
    if selector == "*":
        return discover_skill_links(source, skip_roots=skip_roots)
    return resolve_selector(source, selector, skip_roots=skip_roots)


def list_profile_selected_links(
    config: dict, source: Source, *, skip_roots: set[Path] | None = None
) -> list[tuple[str, Path]]:
    includes = config.get("include") or ["*"]
    excludes = config.get("exclude") or []

    links = []
    for item in includes:
        links.extend(list_selector_links(source, item, skip_roots=skip_roots))

    excluded_paths = set()
    for item in excludes:
        for _name, target in list_selector_links(source, item, skip_roots=skip_roots):
            excluded_paths.add(target.resolve())

    return [
        (name, target)
        for name, target in links
        if target.resolve() not in excluded_paths
    ]


def list_profile_skill_rows(
    root: Path,
    sources: dict[str, Source],
    profile: dict,
    source_filters: list[str] | None,
) -> list[SkillCatalogEntry]:
    selected_sources = (
        set(validate_skill_source_filters(source_filters, sources, root))
        if source_filters
        else None
    )
    rows = []
    workspace = workspace_source(root)
    for source_name, config in profile.get("skill", {}).items():
        if selected_sources is not None and source_name not in selected_sources:
            continue
        source = skill_source(source_name, sources, workspace)
        require_source_path(source)
        skip_roots = skill_skip_roots(source_name, sources)
        for name, target in list_profile_selected_links(
            config or {}, source, skip_roots=skip_roots
        ):
            rows.append(catalog_entry(source_name, source, name, target))
    return rows


def discover_catalog(
    root: Path,
    sources: dict[str, Source],
    *,
    profile: dict | None = None,
    source_filters: list[str] | None = None,
    progress: Progress | None = None,
) -> tuple[SkillCatalogEntry, ...]:
    filters = source_filters or []
    if profile is not None:
        entries = list_profile_skill_rows(root, sources, profile, filters)
    elif filters:
        entries = list_filtered_skill_rows(root, sources, filters)
    else:
        entries = list_all_skill_rows(root, sources, progress=progress)
    return tuple(entries)
