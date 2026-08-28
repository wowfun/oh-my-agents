from __future__ import annotations

import ipaddress
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal, Sequence

import typer

from .common import die, expand_path, read_toml, render_toml, write_toml
from .completion import (
    complete_directory,
    complete_profile,
    complete_profile_remove_reference,
    complete_selector,
    complete_skill_add,
    complete_skill_reference,
    complete_source,
    complete_source_or_workspace,
)
from .model_proxy import ModelProxyConfigError
from .model_proxy.daemon import (
    ModelProxyServiceError,
    restart_model_proxy,
    start_model_proxy,
    stop_model_proxy,
)
from .profiles import (
    build_profile_config,
    default_profile_link_mode,
    discover_skill_dirs,
    discover_skill_links,
    init_profile,
    install_skill,
    list_profile_configs,
    profile_config_path,
    profile_dir_path,
    profile_skill_names,
    profile_source_names,
    read_profile_config,
    remove_profile_directory,
    resolve_selector,
    resolve_profile_skill_reference,
    skill_source,
    source_relative_selector,
    update_profile_config,
    validate_profile_skill_selectors,
    validate_profile_name,
    workspace_source,
    write_profile_config,
)
from .sources import (
    add_source_entry,
    build_source_entry,
    find_profile_source_references,
    infer_owner_source_name_from_url,
    is_git_url,
    raw_source_by_name,
    remove_source_entry,
    require_source_path,
    resolve_sources,
    resolve_source_add_args,
    select_sources,
    SourceCannotFastForwardError,
    SourceSyncError,
    sync_source,
)
from .space.purge import PurgeRequest, edit_purge_paths, purge_space
from .space.render import render_paths_edit_report, render_purge_report
from .workspace import init_workspace, resolve_workspace_root, workspace_config_path


DEFAULT_SKILLS_DIRECTORY = Path(".agents") / "skills"
LinkMode = Literal["symlink", "copy", "junction"]


def resolve_skill_install_dir(
    skills_path: str | None,
    skills_root: str | None,
    global_install: bool,
    cwd: Path,
    *,
    default_root: Path | None,
) -> Path:
    if skills_path is not None:
        return expand_path(skills_path, cwd)

    if skills_root is not None:
        install_root = expand_path(skills_root, cwd)
    elif global_install:
        install_root = Path.home()
    elif default_root is not None:
        install_root = default_root
    else:
        die("skill destination requires --path or --dir")

    return install_root / DEFAULT_SKILLS_DIRECTORY


def require_at_most_one(options: dict[str, object]) -> None:
    selected = [name for name, value in options.items() if value not in (None, False)]
    if len(selected) > 1:
        raise typer.BadParameter(
            f"options are mutually exclusive: {', '.join(selected)}"
        )


def require_exactly_one(options: dict[str, object]) -> None:
    require_at_most_one(options)
    if not any(value not in (None, False) for value in options.values()):
        raise typer.BadParameter(
            f"one of the options is required: {', '.join(options)}"
        )


def parse_source_slice(value: str, total: int) -> list[int]:
    def parse_index(raw: str, label: str) -> int:
        try:
            parsed = int(raw)
        except ValueError:
            die(f"invalid source slice {value!r}: {label} must be a positive integer")
        if parsed <= 0:
            die(f"invalid source slice {value!r}: {label} must be a positive integer")
        return parsed

    indexes: set[int] = set()
    for term in value.split(","):
        if not term:
            die(f"invalid source slice: {value}")
        if ":" in term:
            parts = term.split(":")
            if len(parts) != 2 or (not parts[0] and not parts[1]):
                die(f"invalid source slice: {value}")
            start = 1 if not parts[0] else parse_index(parts[0], "start")
            end = total if not parts[1] else parse_index(parts[1], "end")
        else:
            start = parse_index(term, "index")
            end = start

        if start > end:
            die(f"invalid source slice {value!r}: start must be <= end")
        if start > total or end > total:
            die(f"invalid source slice {value!r}: selected source count is {total}")
        indexes.update(range(start, end + 1))
    return sorted(indexes)


def source_slice_entries(selected: list, value: str | None) -> list[tuple[int, object]]:
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


def format_called_process_error(error: subprocess.CalledProcessError) -> str:
    cmd = error.cmd
    if isinstance(cmd, list | tuple):
        rendered_cmd = " ".join(str(part) for part in cmd)
    else:
        rendered_cmd = str(cmd)
    details = (error.stderr or error.output or "").strip()
    if details:
        return f"command failed with exit {error.returncode}: {rendered_cmd}: {details}"
    return f"command failed with exit {error.returncode}: {rendered_cmd}"


def dedupe_preserve_order(values: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def workspace_root_arg(value: str | None) -> Path:
    return resolve_workspace_root(value, Path.cwd())


def load_registry(root: Path) -> dict:
    return read_toml(workspace_config_path(root))


def load_sources(root: Path, checkout_dir: str | None) -> dict:
    registry = load_registry(root)
    return resolve_sources(registry, repo_root=root, checkout_override=checkout_dir)


def default_sync_depth(registry: dict) -> int | None:
    depth = registry.get("defaults", {}).get("depth")
    if depth is None:
        return None
    if isinstance(depth, bool) or not isinstance(depth, int) or depth <= 0:
        die("defaults.depth must be a positive integer")
    return depth


def init_workspace_command(*, root: str | None, force: bool, dry_run: bool) -> None:
    init_workspace(root, Path.cwd(), force=force, dry_run=dry_run)


def model_proxy_config_path(
    *, model_proxy: bool, root: str | None, config: str | None
) -> Path:
    if not model_proxy:
        raise typer.BadParameter("--model-proxy is required")
    require_at_most_one({"--root": root, "--config": config})
    if config is not None:
        return expand_path(config, Path.cwd())
    return workspace_root_arg(root) / "hagency-model-proxy.toml"


def validate_model_proxy_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        ) from exc
    if not address.is_loopback:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        )


def model_proxy_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def start_model_proxy_command(
    *,
    model_proxy: bool,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
) -> None:
    config_path = model_proxy_config_path(
        model_proxy=model_proxy, root=root, config=config
    )
    validate_model_proxy_host(host)
    try:
        state, paths = start_model_proxy(config_path, host=host, port=port)
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"started model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")


def stop_model_proxy_command(
    *, model_proxy: bool, root: str | None, config: str | None
) -> None:
    config_path = model_proxy_config_path(
        model_proxy=model_proxy, root=root, config=config
    )
    try:
        stopped, _paths = stop_model_proxy(config_path)
    except ModelProxyServiceError as exc:
        die(str(exc))
    print("stopped model proxy" if stopped else "model proxy is not running")


def restart_model_proxy_command(
    *,
    model_proxy: bool,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
) -> None:
    config_path = model_proxy_config_path(
        model_proxy=model_proxy, root=root, config=config
    )
    validate_model_proxy_host(host)
    try:
        state, paths = restart_model_proxy(config_path, host=host, port=port)
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"restarted model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")


def sync_sources_with_progress(
    entries: list[tuple[int, object]],
    *,
    total: int,
    dry_run: bool,
    depth: int | None,
    reanchor: bool = False,
) -> None:
    failures: list[str] = []
    reanchor_candidates: list[str] = []
    for index, source in entries:
        print(f"sync source [{index}/{total}] {source.name}")
        try:
            sync_source(source, dry_run=dry_run, depth=depth, reanchor=reanchor)
        except SourceCannotFastForwardError as exc:
            failures.append(source.name)
            reanchor_candidates.append(source.name)
            print(f"Error: source {source.name} failed: {exc}", file=sys.stderr)
        except SourceSyncError as exc:
            failures.append(source.name)
            print(f"Error: source {source.name} failed: {exc}", file=sys.stderr)
        except subprocess.CalledProcessError as exc:
            failures.append(source.name)
            print(
                f"Error: source {source.name} failed: {format_called_process_error(exc)}",
                file=sys.stderr,
            )

    if failures:
        if reanchor_candidates:
            command = shlex.join(
                ["hgc", "source", "sync", *reanchor_candidates, "--reanchor"]
            )
            print(
                "Tip: if these checkouts are disposable and local-only commits may be discarded, run:\n"
                f"  {command}",
                file=sys.stderr,
            )
        die(f"source sync failed for: {', '.join(failures)}")


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
) -> None:
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
    sync_sources_with_progress(
        source_slice_entries(selected, source_slice),
        total=total,
        dry_run=dry_run,
        depth=sync_depth,
        reanchor=reanchor,
    )


def profile_init_link_mode(copy: bool, link_mode: LinkMode | None) -> str:
    if copy and link_mode in {"symlink", "junction"}:
        die(f"-cp cannot be combined with --link-mode {link_mode}")
    if copy:
        return "copy"
    return link_mode or default_profile_link_mode()


def init_profile_command(
    *,
    name: str,
    skills_path: str | None,
    skills_root: str | None,
    copy: bool,
    link_mode: LinkMode | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
) -> None:
    from .profile_ui import QuestionarySkillConflictUI

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
    init_profile(
        profile,
        sources,
        root,
        skills_dir,
        link_mode=profile_init_link_mode(copy, link_mode),
        dry_run=dry_run,
        conflict_ui=QuestionarySkillConflictUI(),
    )


def skill_skip_roots(source_name: str, sources: dict) -> set[Path] | None:
    if source_name == "workspace":
        return {source.path for source in sources.values()}
    return None


def format_skill_row(source_name: str, source, name: str, target: Path) -> str:
    selector = source_relative_selector(source, target)
    return "\t".join([source_name, name, selector, str(target.resolve())])


def list_all_skill_rows(root: Path, sources: dict) -> list[str]:
    rows = []
    candidates = [("workspace", workspace_source(root)), *sources.items()]
    for source_name, source in candidates:
        if not source.path.exists():
            print(
                f"Warning: skipping missing source {source_name}: {source.path}",
                file=sys.stderr,
            )
            continue
        if not source.path.is_dir():
            print(
                f"Warning: skipping non-directory source {source_name}: {source.path}",
                file=sys.stderr,
            )
            continue
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(format_skill_row(source_name, source, target.name, target))
    return rows


def validate_skill_source_filters(
    source_filters: list[str], sources: dict, root: Path
) -> list[str]:
    selected = dedupe_preserve_order(source_filters)
    available = {"workspace": workspace_source(root), **sources}
    for source_name in selected:
        source = available.get(source_name)
        if source is None:
            die(f"unknown source: {source_name}")
        require_source_path(source)
    return selected


def list_filtered_skill_rows(
    root: Path, sources: dict, source_filters: list[str]
) -> list[str]:
    rows = []
    available = {"workspace": workspace_source(root), **sources}
    for source_name in validate_skill_source_filters(source_filters, sources, root):
        source = available[source_name]
        skip_roots = skill_skip_roots(source_name, sources)
        for target in discover_skill_dirs(source.path, skip_roots=skip_roots):
            rows.append(format_skill_row(source_name, source, target.name, target))
    return rows


def list_selector_links(
    source, selector: str, *, skip_roots: set[Path] | None = None
) -> list[tuple[str, Path]]:
    if selector == "*":
        return discover_skill_links(source, skip_roots=skip_roots)
    return resolve_selector(source, selector, skip_roots=skip_roots)


def list_profile_selected_links(
    config: dict, source, *, skip_roots: set[Path] | None = None
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
    root: Path, sources: dict, profile: dict, source_filters: list[str] | None
) -> list[str]:
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
            rows.append(format_skill_row(source_name, source, name, target))
    return rows


def skill_list_command(
    *,
    source_filters: list[str],
    profile_name: str | None,
    root_value: str | None,
    checkout_dir: str | None,
) -> None:
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)

    if profile_name:
        profile = read_profile_config(root, profile_name)
        rows = list_profile_skill_rows(root, sources, profile, source_filters)
    elif source_filters:
        rows = list_filtered_skill_rows(root, sources, source_filters)
    else:
        rows = list_all_skill_rows(root, sources)

    print("source\tname\tselector\tpath")
    for row in rows:
        print(row)


def resolve_skill_add_link(
    reference: str, sources: dict, root: Path
) -> tuple[str, Path]:
    source_name, selector = resolve_profile_skill_reference(
        reference,
        sources,
        root,
        command_prefix="hgc skill",
        option="add",
    )
    if selector is None:
        die(f"skill add requires one skill, not source: {reference}")

    source = skill_source(source_name, sources, workspace_source(root))
    require_source_path(source)
    links = resolve_selector(
        source, selector, skip_roots=skill_skip_roots(source_name, sources)
    )
    if len(links) != 1:
        die(
            f"skill reference {reference!r} matched {len(links)} skills; choose one exact SOURCE:selector"
        )
    return links[0]


def skill_add_command(
    *,
    skill: str,
    skills_path: str | None,
    skills_root: str | None,
    global_install: bool,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
) -> None:
    invocation_cwd = Path.cwd()
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)
    name, target = resolve_skill_add_link(skill, sources, root)
    skills_dir = resolve_skill_install_dir(
        skills_path,
        skills_root,
        global_install,
        invocation_cwd,
        default_root=invocation_cwd,
    )
    install_skill(
        skills_dir,
        name,
        target,
        link_mode=default_profile_link_mode(),
        dry_run=dry_run,
    )


def profile_list_command(*, root_value: str | None) -> None:
    root = workspace_root_arg(root_value)
    print("name\tdescription\tskills")
    for name, profile in list_profile_configs(root):
        description = profile.get("description") or "-"
        skills = ",".join(profile_skill_names(profile)) or "-"
        print(f"{name}\t{description}\t{skills}")


def profile_show_command(*, name: str, root_value: str | None) -> None:
    root = workspace_root_arg(root_value)
    profile = read_profile_config(root, name)
    print(render_toml(profile).rstrip())


def validate_profile_skill_args(
    include: list[str] | None,
    exclude: list[str] | None,
    add_skill: str | None,
) -> tuple[list[str] | None, list[str] | None]:
    if (include or exclude) and not add_skill:
        die("--include and --exclude require --add-skill")
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


def profile_add_command(
    *,
    name: str,
    description: str | None,
    add_skill: str | None,
    include: list[str] | None,
    exclude: list[str] | None,
    root_value: str | None,
    checkout_dir: str | None,
    dry_run: bool,
) -> None:
    root = workspace_root_arg(root_value)
    validate_profile_name(name)
    profile_dir = profile_dir_path(root, name)
    if profile_dir.exists():
        die(f"profile already exists: {name}")
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    sources = load_sources(root, checkout_dir) if add_skill else {}
    if add_skill:
        add_skill, inferred_include = resolve_profile_skill_reference(
            add_skill,
            sources,
            root,
            command_prefix=f"hgc profile add {name}",
            option="-AS",
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
        print(f"Would create profile: {profile_config_path(root, name)}")
        print(render_toml(profile).rstrip())
        return

    write_profile_config(root, name, profile)
    print(f"added profile: {name}")


def profile_update_command(
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
) -> None:
    root = workspace_root_arg(root_value)
    include, exclude = validate_profile_skill_args(include, exclude, add_skill)
    if replace and not add_skill:
        die("--replace requires --add-skill")
    profile = read_profile_config(root, name)
    sources = load_sources(root, checkout_dir) if add_skill or remove_skill else {}
    if add_skill:
        add_skill, inferred_include = resolve_profile_skill_reference(
            add_skill,
            sources,
            root,
            command_prefix=f"hgc profile update {name}",
            option="-AS",
        )
        include = with_inferred_include(include, inferred_include)
        validate_profile_skill_selectors(
            add_skill, sources, root, include=include, exclude=exclude
        )
    remove_skill_selector = None
    if remove_skill:
        remove_skill, inferred_remove = resolve_profile_skill_reference(
            remove_skill,
            sources,
            root,
            command_prefix=f"hgc profile update {name}",
            option="-RS",
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
        print(f"Would update profile: {profile_config_path(root, name)}")
        print(render_toml(updated).rstrip())
        return

    write_profile_config(root, name, updated)
    print(f"updated profile: {name}")


def profile_remove_command(*, name: str, root_value: str | None, dry_run: bool) -> None:
    root = workspace_root_arg(root_value)
    profile_dir = profile_dir_path(root, name)
    if not profile_dir.exists():
        die(f"unknown profile: {name}")

    if dry_run:
        print(f"Would remove profile directory: {profile_dir}")
        return

    remove_profile_directory(root, name)
    print(f"removed profile: {name}")


def source_kind(source) -> str:
    return "remote" if source.remote else "local"


def source_list_command(*, root_value: str | None, checkout_dir: str | None) -> None:
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)
    print("name\ttype\tpath\turl\tref")
    for name, source in sources.items():
        remote = source.remote
        print(
            "\t".join(
                [
                    name,
                    source_kind(source),
                    str(source.path),
                    remote.url if remote else "-",
                    remote.ref if remote else "-",
                ]
            )
        )


def source_show_command(
    *, name: str, root_value: str | None, checkout_dir: str | None
) -> None:
    root = workspace_root_arg(root_value)
    registry = load_registry(root)
    sources = resolve_sources(registry, repo_root=root, checkout_override=checkout_dir)
    source = sources.get(name)
    if source is None:
        die(f"unknown source: {name}")
    raw_source = raw_source_by_name(registry, name) or {}
    remote = source.remote
    raw_remote = raw_source.get("remote") or {}

    print(f"name: {source.name}")
    print(f"type: {source_kind(source)}")
    print(f"resolved_path: {source.path}")
    if "path" in raw_source:
        print(f"path: {raw_source['path']}")
    if remote:
        print(f"remote.url: {raw_remote.get('url', remote.url)}")
        print(f"remote.name: {raw_remote.get('name', remote.name)}")
        print(f"remote.ref: {raw_remote.get('ref', remote.ref)}")


def source_add_command(
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
) -> None:
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
            die(
                f"source already exists: {name}; could not infer owner/repo name from URL; "
                "pass --name <custom-name>"
            )
        if raw_source_by_name(registry, owner_name) is not None:
            die(
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
        print("Would add source:")
        print(render_toml({"source": {source_name: entry}}).rstrip())
        if sync_source_entry is not None:
            sync_sources_with_progress(
                [(1, sync_source_entry)], total=1, dry_run=True, depth=sync_depth
            )
        return

    write_toml(config_path, registry)
    print(f"added source: {source_name}")
    if sync_source_entry is not None:
        sync_sources_with_progress(
            [(1, sync_source_entry)], total=1, dry_run=False, depth=sync_depth
        )


def source_remove_command(
    *, name: str, force: bool, root_value: str | None, dry_run: bool
) -> None:
    root = workspace_root_arg(root_value)
    config_path = workspace_config_path(root)
    registry = load_registry(root)
    resolve_sources(registry, repo_root=root, checkout_override=None)

    references = find_profile_source_references(root, name)
    if references and not force:
        refs = ", ".join(str(path.relative_to(root)) for path in references)
        die(
            f"source {name} is referenced by {refs}; pass --force to remove only the config entry"
        )

    remove_source_entry(registry, name)
    if dry_run:
        print(f"Would remove source: {name}")
        return

    write_toml(config_path, registry)
    print(f"removed source: {name}")


def make_app(*, help_text: str, add_completion: bool) -> typer.Typer:
    return typer.Typer(
        help=help_text,
        add_completion=add_completion,
        context_settings={"help_option_names": ["-h", "--help"]},
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        no_args_is_help=False,
    )


app = make_app(
    help_text="Manage Hagency workspaces, profiles, sources, local services, and disk space.",
    add_completion=True,
)
source_app = make_app(help_text="Manage workspace sources.", add_completion=False)
skill_app = make_app(
    help_text="Manage workspace and source skills.", add_completion=False
)
profile_app = make_app(help_text="Manage profiles.", add_completion=False)
serve_app = make_app(help_text="Manage local Hagency services.", add_completion=False)
space_app = make_app(help_text="Inspect and reclaim disk space.", add_completion=False)

app.add_typer(source_app, name="source")
app.add_typer(source_app, name="s", help="Alias for source.")
app.add_typer(skill_app, name="skill")
app.add_typer(profile_app, name="profile")
app.add_typer(profile_app, name="p", help="Alias for profile.")
app.add_typer(serve_app, name="serve")
app.add_typer(space_app, name="space")


@app.command("init", help="Initialize a Hagency workspace.")
def init_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing workspace config")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    init_workspace_command(root=root, force=force, dry_run=dry_run)


@space_app.command("purge", help="Find and remove rebuildable project artifacts.")
def space_purge_cli(
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            help="Optional directories to scan instead of configured roots",
            metavar="PATH...",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Preview selected removals without changing files"
        ),
    ] = False,
    edit_paths: Annotated[
        bool,
        typer.Option("--paths", help="Edit the configured purge scan directories"),
    ] = False,
) -> None:
    selected_paths = list(paths or [])
    if edit_paths and (selected_paths or dry_run):
        raise typer.BadParameter(
            "--paths cannot be combined with PATH or --dry-run", param_hint="--paths"
        )

    try:
        if edit_paths:
            report = edit_purge_paths()
            render_paths_edit_report(report)
        else:
            from .space.questionary_ui import QuestionaryPurgeUI

            request = PurgeRequest(
                paths=tuple(
                    Path(os.path.abspath(expand_path(value, Path.cwd())))
                    for value in selected_paths
                ),
                dry_run=dry_run,
            )
            report = purge_space(request, ui=QuestionaryPurgeUI())
            render_purge_report(report)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None

    if report.exit_code:
        raise typer.Exit(report.exit_code)


@serve_app.command("start", help="Start a service in the background.")
def serve_start_cli(
    model_proxy: Annotated[
        bool,
        typer.Option("--model-proxy", help="Serve the provider-level LLM model proxy"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_directory,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
) -> None:
    start_model_proxy_command(
        model_proxy=model_proxy, root=root, config=config, host=host, port=port
    )


@serve_app.command("stop", help="Stop a background service.")
def serve_stop_cli(
    model_proxy: Annotated[
        bool,
        typer.Option("--model-proxy", help="Select the provider-level LLM model proxy"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    stop_model_proxy_command(model_proxy=model_proxy, root=root, config=config)


@serve_app.command("restart", help="Restart a service in the background.")
def serve_restart_cli(
    model_proxy: Annotated[
        bool,
        typer.Option("--model-proxy", help="Select the provider-level LLM model proxy"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_directory,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
) -> None:
    restart_model_proxy_command(
        model_proxy=model_proxy, root=root, config=config, host=host, port=port
    )


@source_app.command("ls", help="Alias for list.")
@source_app.command("list", help="List configured sources.")
def source_list_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    source_list_command(root_value=root, checkout_dir=checkout_dir)


@source_app.command("show", help="Show one configured source.")
def source_show_cli(
    name: Annotated[
        str, typer.Argument(help="Source name", autocompletion=complete_source)
    ],
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    source_show_command(name=name, root_value=root, checkout_dir=checkout_dir)


@source_app.command(
    "add",
    help="Add a source to the workspace config. Pass a Git URL directly to infer the source name.",
)
def source_add_cli(
    source: Annotated[
        str, typer.Argument(help="Source name, or Git URL to infer the name from")
    ],
    name: Annotated[
        str | None,
        typer.Option(
            "--name", help="Override inferred source name when source is a Git URL"
        ),
    ] = None,
    url: Annotated[str | None, typer.Option("--url", help="Git remote URL")] = None,
    path: Annotated[
        str | None,
        typer.Option(
            "--path",
            help="Explicit local or checkout path",
            autocompletion=complete_directory,
        ),
    ] = None,
    ref: Annotated[
        str | None, typer.Option("--ref", help="Git branch, tag, or ref")
    ] = None,
    remote_name: Annotated[
        str | None, typer.Option("--remote-name", help="Git remote name")
    ] = None,
    sync: Annotated[
        bool,
        typer.Option("--sync", help="Sync the added source after writing the config"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    source_add_command(
        source_value=source,
        name_value=name,
        url_value=url,
        path_value=path,
        ref_value=ref,
        remote_name=remote_name,
        sync=sync,
        root_value=root,
        dry_run=dry_run,
    )


@source_app.command("rm", help="Alias for remove.")
@source_app.command("remove", help="Remove a source from the workspace config.")
def source_remove_cli(
    name: Annotated[
        str, typer.Argument(help="Source name", autocompletion=complete_source)
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Remove even if profiles reference the source"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    source_remove_command(name=name, force=force, root_value=root, dry_run=dry_run)


@source_app.command("sync", help="Sync external sources.")
def source_sync_cli(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Optional source names to sync", autocompletion=complete_source
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="Sync only sources referenced by profiles/<name>/config.toml",
            autocompletion=complete_profile,
        ),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option(
            "--depth", min=1, help="Create or update shallow checkouts with this depth"
        ),
    ] = None,
    source_slice: Annotated[
        str | None,
        typer.Option(
            "--slice",
            "-s",
            help="1-based source indexes or slices to sync, such as 4:, 2:4, :3, 4, or 1,3:",
        ),
    ] = None,
    reanchor: Annotated[
        bool,
        typer.Option(
            "--reanchor",
            help="Replace clean local branches when fetched upstream history cannot fast-forward",
        ),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    sync_selected_sources(
        names=list(names or []),
        profile_name=profile,
        depth=depth,
        source_slice=source_slice,
        reanchor=reanchor,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
    )


@skill_app.command("add", help="Install one discovered skill.")
def skill_add_cli(
    skill: Annotated[
        str,
        typer.Argument(
            help="Unique skill name or exact SOURCE:selector",
            autocompletion=complete_skill_add,
        ),
    ],
    skills_path: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            metavar="PATH",
            help="Exact skills directory; no .agents/skills suffix is added",
            autocompletion=complete_directory,
        ),
    ] = None,
    skills_root: Annotated[
        str | None,
        typer.Option(
            "--dir",
            "-d",
            metavar="DIR",
            help="Target workspace directory; install under DIR/.agents/skills",
            autocompletion=complete_directory,
        ),
    ] = None,
    global_install: Annotated[
        bool,
        typer.Option("--global", help="Install under ~/.agents/skills"),
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    require_at_most_one(
        {"--path": skills_path, "--dir": skills_root, "--global": global_install}
    )
    skill_add_command(
        skill=skill,
        skills_path=skills_path,
        skills_root=skills_root,
        global_install=global_install,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
    )


@skill_app.command("ls", help="Alias for list.")
@skill_app.command("list", help="List discovered skills.")
def skill_list_cli(
    sources: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="Limit to a source name or workspace",
            autocompletion=complete_source_or_workspace,
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Limit to skills selected by a profile",
            autocompletion=complete_profile,
        ),
    ] = None,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    skill_list_command(
        source_filters=list(sources or []),
        profile_name=profile,
        root_value=root,
        checkout_dir=checkout_dir,
    )


@profile_app.command("ls", help="Alias for list.")
@profile_app.command("list", help="List profiles.")
def profile_list_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    profile_list_command(root_value=root)


@profile_app.command("add", help="Add a profile.")
def profile_add_cli(
    name: Annotated[str, typer.Argument(help="Profile name under profiles/")],
    description: Annotated[
        str | None, typer.Option("--description", help="Profile description")
    ] = None,
    add_skill: Annotated[
        str | None,
        typer.Option(
            "-AS",
            "--add-skill",
            help="Source, skill name, or SOURCE:selector to add to this profile",
            autocompletion=complete_skill_reference,
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "-i",
            help="Skill selectors to include",
            autocompletion=complete_selector,
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            "-e",
            help="Skill selectors to exclude",
            autocompletion=complete_selector,
        ),
    ] = None,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    profile_add_command(
        name=name,
        description=description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        root_value=root,
        checkout_dir=None,
        dry_run=dry_run,
    )


@profile_app.command("u", help="Alias for update.")
@profile_app.command("update", help="Update a profile.")
def profile_update_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    description: Annotated[
        str | None, typer.Option("--description", help="Profile description")
    ] = None,
    add_skill: Annotated[
        str | None,
        typer.Option(
            "-AS",
            "--add-skill",
            help="Source, skill name, or SOURCE:selector to add or merge",
            autocompletion=complete_skill_reference,
        ),
    ] = None,
    remove_skill: Annotated[
        str | None,
        typer.Option(
            "-RS",
            "--remove-skill",
            help="Source, skill name, or SOURCE:selector to remove",
            autocompletion=complete_profile_remove_reference,
        ),
    ] = None,
    include: Annotated[
        list[str] | None,
        typer.Option(
            "--include",
            "-i",
            help="Skill selectors to include",
            autocompletion=complete_selector,
        ),
    ] = None,
    exclude: Annotated[
        list[str] | None,
        typer.Option(
            "--exclude",
            "-e",
            help="Skill selectors to exclude",
            autocompletion=complete_selector,
        ),
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace one profile skill entry")
    ] = False,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    require_at_most_one({"--add-skill": add_skill, "--remove-skill": remove_skill})
    profile_update_command(
        name=name,
        description=description,
        add_skill=add_skill,
        remove_skill=remove_skill,
        include=include,
        exclude=exclude,
        replace=replace,
        root_value=root,
        checkout_dir=None,
        dry_run=dry_run,
    )


@profile_app.command("rm", help="Alias for remove.")
@profile_app.command("remove", help="Remove a profile.")
def profile_remove_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    profile_remove_command(name=name, root_value=root, dry_run=dry_run)


@profile_app.command("show", help="Show one profile config.")
def profile_show_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    profile_show_command(name=name, root_value=root)


@profile_app.command(
    "init",
    help="Initialize profile skills into a target directory.",
    epilog="Migration: replace previous -p WORKSPACE usage with -d WORKSPACE.",
)
def profile_init_cli(
    name: Annotated[
        str,
        typer.Argument(
            help="Profile name under profiles/", autocompletion=complete_profile
        ),
    ],
    skills_path: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            metavar="PATH",
            help="Exact skills directory; no .agents/skills suffix is added",
            autocompletion=complete_directory,
        ),
    ] = None,
    skills_root: Annotated[
        str | None,
        typer.Option(
            "--dir",
            "-d",
            metavar="DIR",
            help="Target workspace directory; install under DIR/.agents/skills",
            autocompletion=complete_directory,
        ),
    ] = None,
    copy: Annotated[
        bool, typer.Option("-cp", help="Copy skill directories instead of linking")
    ] = False,
    link_mode: Annotated[
        LinkMode | None,
        typer.Option(
            "--link-mode",
            help="How to materialize profile skills; defaults to junction on Windows and symlink elsewhere",
        ),
    ] = None,
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the configured checkout directory",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    require_exactly_one({"--path": skills_path, "--dir": skills_root})
    init_profile_command(
        name=name,
        skills_path=skills_path,
        skills_root=skills_root,
        copy=copy,
        link_mode=link_mode,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
    )


def normalize_legacy_multi_value_options(args: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    index = 0
    while index < len(args):
        option_token = args[index]
        normalized.append(option_token)
        index += 1
        option, separator, _inline_value = option_token.partition("=")
        if option not in {"-i", "--include", "-e", "--exclude"}:
            continue
        has_value = bool(separator)
        while index < len(args) and not args[index].startswith("-"):
            if has_value:
                normalized.append(option)
            normalized.append(args[index])
            index += 1
            has_value = True
    return normalized


def explicit_completion_shell(args: Sequence[str]) -> str | None:
    for index, arg in enumerate(args):
        option, separator, inline_value = arg.partition("=")
        if option not in {"--install-completion", "--show-completion"}:
            continue
        if separator:
            return inline_value
        if index + 1 < len(args) and not args[index + 1].startswith("-"):
            return args[index + 1]
    return None


def main(args: Sequence[str] | None = None) -> None:
    raw_args = list(sys.argv[1:] if args is None else args)
    completion_shell = explicit_completion_shell(raw_args)
    previous_detection = os.environ.get("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION")
    if completion_shell is not None:
        # Typer 0.27 exposes shell-valued completion options when automatic
        # shell detection is disabled, which supports --show-completion SHELL.
        os.environ["_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"] = "1"
    try:
        app(args=normalize_legacy_multi_value_options(raw_args), prog_name="hgc")
    finally:
        if completion_shell is not None:
            if previous_detection is None:
                os.environ.pop("_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION", None)
            else:
                os.environ["_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"] = (
                    previous_detection
                )
