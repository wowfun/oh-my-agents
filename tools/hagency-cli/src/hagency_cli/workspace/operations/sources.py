from __future__ import annotations

import subprocess

from hagency_cli.workspace.config import render_toml, write_toml
from hagency_cli.workspace.context import (
    default_sync_depth,
    load_registry,
    workspace_root_arg,
)
from hagency_cli.workspace.discovery import workspace_config_path
from hagency_cli.workspace.errors import (
    GitCommandError,
    SourceBatchError,
    fail,
    format_called_process_error,
)
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.profiles import (
    find_profile_source_references,
    profile_source_names,
    read_profile_config,
)
from hagency_cli.workspace.sources import (
    Source,
    SourceCannotFastForwardError,
    SourceSyncError,
    add_source_entry,
    build_source_entry,
    infer_owner_source_name_from_url,
    is_git_url,
    raw_source_by_name,
    remove_source_entry,
    resolve_source_add_args,
    resolve_sources,
    select_sources,
    sync_source,
)


def parse_source_slice(value: str, total: int) -> list[int]:
    def parse_index(raw: str, label: str) -> int:
        try:
            parsed = int(raw)
        except ValueError:
            fail(f"invalid source slice {value!r}: {label} must be a positive integer")
        if parsed <= 0:
            fail(f"invalid source slice {value!r}: {label} must be a positive integer")
        return parsed

    indexes: set[int] = set()
    for term in value.split(","):
        if not term:
            fail(f"invalid source slice: {value}")
        if ":" in term:
            parts = term.split(":")
            if len(parts) != 2 or (not parts[0] and not parts[1]):
                fail(f"invalid source slice: {value}")
            start = 1 if not parts[0] else parse_index(parts[0], "start")
            end = total if not parts[1] else parse_index(parts[1], "end")
        else:
            start = parse_index(term, "index")
            end = start

        if start > end:
            fail(f"invalid source slice {value!r}: start must be <= end")
        if start > total or end > total:
            fail(f"invalid source slice {value!r}: selected source count is {total}")
        indexes.update(range(start, end + 1))
    return sorted(indexes)


def source_slice_entries(
    selected: list[Source], value: str | None
) -> list[tuple[int, Source]]:
    total = len(selected)
    if value is None:
        indexes = set(range(1, total + 1))
    else:
        indexes = set(parse_source_slice(value, total))
    return [
        (index, source)
        for index, source in enumerate(selected, start=1)
        if index in indexes
    ]


def sync_sources_with_progress(
    entries: list[tuple[int, Source]],
    *,
    total: int,
    dry_run: bool,
    depth: int | None,
    reanchor: bool = False,
    progress: Progress | None = None,
) -> tuple[Source, ...]:
    failures: list[str] = []
    reanchor_candidates: list[str] = []
    for index, source in entries:
        emit_event(progress, f"sync source [{index}/{total}] {source.name}")
        try:
            sync_source(
                source,
                dry_run=dry_run,
                depth=depth,
                reanchor=reanchor,
                progress=progress,
            )
        except SourceCannotFastForwardError as exc:
            failures.append(source.name)
            reanchor_candidates.append(source.name)
            emit_event(
                progress, f"Error: source {source.name} failed: {exc}", error=True
            )
        except (SourceSyncError, GitCommandError) as exc:
            failures.append(source.name)
            emit_event(
                progress, f"Error: source {source.name} failed: {exc}", error=True
            )
        except subprocess.CalledProcessError as exc:
            failures.append(source.name)
            emit_event(
                progress,
                f"Error: source {source.name} failed: {format_called_process_error(exc)}",
                error=True,
            )

    if failures:
        raise SourceBatchError(tuple(failures), tuple(reanchor_candidates))
    return tuple(source for _, source in entries)


def sync_selected_sources(
    *,
    names: list[str],
    profile_name: str | None,
    depth: int | None,
    source_slice: str | None,
    reanchor: bool,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> tuple[Source, ...]:
    root = workspace_root_arg(root_value)
    registry = load_registry(root)
    sources = resolve_sources(registry, repo_root=root, checkout_override=checkout_dir)
    sync_depth = depth if depth is not None else default_sync_depth(registry)

    selected_names = list(names)
    if profile_name:
        profile = read_profile_config(root, profile_name)
        selected_names.extend(profile_source_names(profile))

    selected = (
        select_sources(sources, selected_names)
        if selected_names
        else list(sources.values())
    )
    total = len(selected)
    return sync_sources_with_progress(
        source_slice_entries(selected, source_slice),
        total=total,
        dry_run=dry_run,
        depth=sync_depth,
        reanchor=reanchor,
        progress=progress,
    )


def add_source(
    *,
    source_value: str,
    name_value: str | None,
    url_value: str | None,
    path_value: str | None,
    ref_value: str | None,
    remote_name: str | None,
    sync: bool,
    root_value: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> str:
    root = workspace_root_arg(root_value)
    config_path = workspace_config_path(root)
    registry = load_registry(root)
    resolve_sources(registry, repo_root=root, checkout_override=None)
    name, url = resolve_source_add_args(source_value, name=name_value, url=url_value)
    if (
        is_git_url(source_value)
        and name_value is None
        and url_value is None
        and raw_source_by_name(registry, name) is not None
    ):
        owner_name = infer_owner_source_name_from_url(source_value)
        if not owner_name or owner_name == name:
            fail(
                f"source already exists: {name}; could not infer owner/repo name from URL; "
                "pass --name <custom-name>"
            )
        if raw_source_by_name(registry, owner_name) is not None:
            fail(
                f"source already exists: {name}; owner-prefixed source also exists: {owner_name}; "
                "pass --name <custom-name>"
            )
        name = owner_name
    source_name, entry = build_source_entry(
        name=name,
        url=url,
        path=path_value,
        ref=ref_value,
        remote_name=remote_name,
    )
    add_source_entry(registry, source_name, entry)
    sync_source_entry = None
    sync_depth = None
    if sync:
        sync_depth = default_sync_depth(registry)
        sync_source_entry = select_sources(
            resolve_sources(registry, repo_root=root, checkout_override=None),
            [source_name],
        )[0]

    if dry_run:
        emit_event(progress, "Would add source:")
        emit_event(progress, render_toml({"source": {source_name: entry}}).rstrip())
        if sync_source_entry is not None:
            sync_sources_with_progress(
                [(1, sync_source_entry)],
                total=1,
                dry_run=True,
                depth=sync_depth,
                progress=progress,
            )
        return source_name

    write_toml(config_path, registry)
    emit_event(progress, f"added source: {source_name}")
    if sync_source_entry is not None:
        sync_sources_with_progress(
            [(1, sync_source_entry)],
            total=1,
            dry_run=False,
            depth=sync_depth,
            progress=progress,
        )
    return source_name


def remove_source(
    *,
    name: str,
    force: bool,
    root_value: str | None,
    dry_run: bool,
    progress: Progress | None = None,
) -> str:
    root = workspace_root_arg(root_value)
    config_path = workspace_config_path(root)
    registry = load_registry(root)
    resolve_sources(registry, repo_root=root, checkout_override=None)

    references = find_profile_source_references(root, name)
    if references and not force:
        refs = ", ".join(str(path.relative_to(root)) for path in references)
        fail(
            f"source {name} is referenced by {refs}; pass --force to remove only the config entry"
        )

    remove_source_entry(registry, name)
    if dry_run:
        emit_event(progress, f"Would remove source: {name}")
        return name

    write_toml(config_path, registry)
    emit_event(progress, f"removed source: {name}")
    return name
