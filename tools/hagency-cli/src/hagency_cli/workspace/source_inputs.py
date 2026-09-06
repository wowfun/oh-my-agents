"""Resolve installation inputs to persistent sources without changing the workspace."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from hagency_cli.paths import expand_path
from hagency_cli.workspace.errors import fail
from hagency_cli.workspace.sources import (
    Source,
    build_source_entry,
    infer_owner_source_name_from_url,
    infer_source_name_from_url,
    resolve_sources,
    validate_git_url,
    validate_source_name,
)


@dataclass(frozen=True)
class SkillInput:
    kind: Literal["path", "source", "selector", "url", "name"]
    value: str
    selector: str | None = None


@dataclass(frozen=True)
class SourceSelection:
    source: Source
    scope_root: Path
    new_entry: dict | None = None


def is_explicit_path(value: str) -> bool:
    return (
        value in {".", "..", "~"}
        or value.startswith(("./", "../", ".\\", "..\\", "~/", "~\\", "/", "\\"))
        or bool(PureWindowsPath(value).drive)
    )


def classify_skill_input(value: str, sources: dict[str, Source]) -> SkillInput:
    source, separator, selector = value.partition(":")
    named_selector = (
        separator and source in sources and not selector.startswith(("/", "\\"))
    )
    if is_explicit_path(value) and not named_selector:
        return SkillInput("path", value)
    if separator and (source == "workspace" or source in sources):
        if not selector:
            fail(f"skill reference {value!r} is missing selector after ':'")
        return SkillInput("selector", source, selector)
    if value == "workspace" or value in sources:
        return SkillInput("source", value)
    if "://" in value or re.match(r"[^\s/@:]+@[^\s/:]+:", value):
        validate_git_url(value)
        return SkillInput("url", value)
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        if any(part in {".", ".."} for part in value.split("/")):
            fail(f"invalid GitHub repository: {value}")
        return SkillInput("url", f"https://github.com/{value.removesuffix('.git')}.git")
    return SkillInput("name", value)


def remote_identity(value: str) -> str:
    """Normalize GitHub suffixes, without merging transports or other Git hosts."""
    parsed = urlsplit(value)
    if parsed.hostname == "github.com":
        path = parsed.path.rstrip("/").removesuffix(".git")
        return urlunsplit(
            (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
        )
    if re.match(r"[^\s/@:]+@github\.com:", value):
        return value.rstrip("/").removesuffix(".git")
    return value


def _new_source(
    *, name: str, entry: dict, registry: dict, root: Path, checkout_dir: str | None
) -> SourceSelection:
    validate_source_name(name)
    if name in registry.get("source", {}):
        fail(f"source already exists: {name}; choose another --source-name")
    proposed = {**registry, "source": {**registry.get("source", {}), name: entry}}
    source = resolve_sources(proposed, repo_root=root, checkout_override=checkout_dir)[
        name
    ]
    if source.remote is not None:
        if source.path.exists() or source.path.is_symlink():
            fail(
                f"unregistered checkout path is occupied: {source.path}; choose --source-name or --checkout-dir, or register it explicitly"
            )
        # Validate the filesystem ancestor before persisting a source that cannot
        # be checked out. Do not follow dangling links into a new checkout.
        for parent in source.path.parents:
            if parent.is_symlink() and not parent.exists():
                fail(f"checkout ancestor is a broken symlink: {parent}")
            if parent.exists():
                if not parent.is_dir():
                    fail(f"checkout ancestor is not a directory: {parent}")
                break
        if checkout_dir is not None:
            entry = {**entry, "path": str(source.path.resolve())}
            source = Source(source.name, source.path.resolve(), source.remote)
    return SourceSelection(source, source.path, entry)


def resolve_remote_input(
    url: str,
    *,
    source_name: str | None,
    ref: str | None,
    sources: dict[str, Source],
    registry: dict,
    root: Path,
    checkout_dir: str | None,
) -> SourceSelection:
    validate_git_url(url)
    matches = [
        source
        for source in sources.values()
        if source.remote is not None
        and remote_identity(source.remote.url) == remote_identity(url)
        and (ref is None or source.remote.ref == ref)
    ]
    if source_name is not None:
        validate_source_name(source_name)
        if source_name in sources:
            if sources[source_name] not in matches:
                fail(
                    f"source {source_name} does not match the requested URL/ref; existing sources are never retargeted"
                )
            source = sources[source_name]
            return SourceSelection(source, source.path)
    elif len(matches) == 1:
        return SourceSelection(matches[0], matches[0].path)
    elif len(matches) > 1:
        fail(
            "multiple sources match this URL/ref: "
            + ", ".join(s.name for s in matches)
            + "; use --source-name"
        )
    name = source_name or infer_source_name_from_url(url)
    if source_name is None and name in sources:
        name = infer_owner_source_name_from_url(url) or name
    _, entry = build_source_entry(
        name=name, url=url, path=None, ref=ref, remote_name=None
    )
    return _new_source(
        name=name, entry=entry, registry=registry, root=root, checkout_dir=checkout_dir
    )


def resolve_local_input(
    value: str,
    *,
    cwd: Path,
    source_name: str | None,
    sources: dict[str, Source],
    registry: dict,
    root: Path,
) -> SourceSelection:
    path = expand_path(value, cwd).resolve()
    if not path.is_dir():
        fail(f"local source is not a directory: {path}")
    matches = [
        source
        for source in sources.values()
        if path.is_relative_to(source.path.resolve())
    ]
    if source_name is not None:
        if source_name == "workspace":
            if not path.is_relative_to(root.resolve()):
                fail(f"local path is outside workspace: {path}")
            return SourceSelection(Source("workspace", root, None), path)
        validate_source_name(source_name)
        if source_name in sources:
            if sources[source_name] not in matches:
                fail(f"local path is outside source {source_name}: {path}")
            return SourceSelection(sources[source_name], path)
    elif matches:
        depth = max(len(source.path.resolve().parts) for source in matches)
        matches = [
            source for source in matches if len(source.path.resolve().parts) == depth
        ]
        if len(matches) != 1:
            fail(
                "multiple sources contain this path: "
                + ", ".join(s.name for s in matches)
                + "; use --source-name"
            )
        return SourceSelection(matches[0], path)
    elif path.is_relative_to(root.resolve()):
        return SourceSelection(Source("workspace", root, None), path)
    return _new_source(
        name=source_name or path.name,
        entry={"path": str(path)},
        registry=registry,
        root=root,
        checkout_dir=None,
    )
