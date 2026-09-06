from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from hagency_cli.paths import expand_path
from hagency_cli.workspace.errors import SourceNotReadyError, fail
from hagency_cli.workspace.events import Progress, emit_event
from hagency_cli.workspace.git import git_ok, git_process, run

GIT_NETWORK_RETRIES = 3


GIT_NETWORK_RETRY_DELAY_SECONDS = 0.25


GIT_SHALLOW_DEEPEN_STEPS = (50, 200, 1000)


class SourceSyncError(RuntimeError):
    pass


class SourceCannotFastForwardError(SourceSyncError):
    pass


@dataclass(frozen=True)
class Remote:
    name: str
    url: str
    ref: str


@dataclass(frozen=True)
class Source:
    name: str
    path: Path
    remote: Remote | None


def configured_checkout_dir(
    defaults: dict,
    *,
    checkout_override: str | None,
    windows: bool,
) -> str | None:
    if checkout_override:
        return checkout_override
    if windows:
        return defaults.get("checkout_dir_windows") or defaults.get("checkout_dir")
    return defaults.get("checkout_dir")


def resolve_sources(
    registry: dict, *, repo_root: Path, checkout_override: str | None
) -> dict[str, Source]:
    if "sources" in registry:
        fail("legacy [[sources]] config is no longer supported; use [source.<name>]")
    defaults = registry.get("defaults", {})
    if not isinstance(defaults, dict):
        fail("defaults must be a table")
    for field in ("checkout_dir", "checkout_dir_windows", "remote_name", "remote_ref"):
        if field in defaults and not isinstance(defaults[field], str):
            fail(f"defaults.{field} must be a string")
    source_entries = registry.get("source", {})
    if not isinstance(source_entries, dict):
        fail("source must be a table")
    checkout_dir_value = configured_checkout_dir(
        defaults,
        checkout_override=checkout_override,
        windows=os.name == "nt",
    )
    checkout_dir = (
        expand_path(checkout_dir_value, repo_root) if checkout_dir_value else None
    )
    default_remote_name = defaults.get("remote_name", "origin")
    default_remote_ref = defaults.get("remote_ref", "main")

    sources: dict[str, Source] = {}
    for name, raw_source in source_entries.items():
        if name in sources:
            fail(f"duplicate source name: {name}")

        if not isinstance(raw_source, dict):
            fail(f"source {name} must be a table")
        if "path" in raw_source and not isinstance(raw_source["path"], str):
            fail(f"source {name} path must be a string")
        raw_remote = raw_source.get("remote")
        if raw_remote is not None:
            if not isinstance(raw_remote, dict):
                fail(f"source {name} remote must be a table")
            for field in ("url", "name", "ref"):
                if field in raw_remote and not isinstance(raw_remote[field], str):
                    fail(f"source {name} remote.{field} must be a string")
        remote = None
        if raw_remote:
            url = raw_remote.get("url")
            if not url:
                fail(f"source {name} remote is missing required field: url")
            validate_git_url(url, allow_local=True)
            remote = Remote(
                name=raw_remote.get("name") or default_remote_name,
                url=url,
                ref=raw_remote.get("ref") or default_remote_ref,
            )

        raw_path = raw_source.get("path")
        if raw_path:
            path = expand_path(raw_path, repo_root)
        elif remote and checkout_dir:
            path = checkout_dir / name
        else:
            fail(
                f"source {name} needs path, or remote plus a configured checkout directory"
            )

        sources[name] = Source(
            name=name,
            path=path,
            remote=remote,
        )

    return sources


def raw_source_by_name(registry: dict, name: str) -> dict | None:
    return registry.get("source", {}).get(name)


def validate_git_url(value: str, *, allow_local: bool = False) -> None:
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail("Git repository addresses cannot be empty or contain control characters")
    if (
        allow_local
        and "://" not in value
        and not re.match(r"[^\s/@:]+@[^\s/:]+:", value)
    ):
        # Explicit source registration also accepts Git's local clone addresses.
        # Keep paths containing spaces, including native Windows paths.
        if value.startswith("-"):
            fail("Git repository addresses cannot start with '-'")
        return
    if any(c.isspace() for c in value):
        fail("Git repository URLs cannot contain whitespace or control characters")
    if re.match(r"[^\s/@:]+@[^\s/:]+:.+", value):
        return
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port  # Validate a malformed authority before registering a source.
    except ValueError as exc:
        fail(f"invalid Git repository URL: {value}: {exc}")
    if parsed.scheme not in {"https", "http", "ssh", "git", "file"}:
        fail(f"unsupported Git URL: {value}")
    if not parsed.path or (parsed.scheme != "file" and not hostname):
        fail(f"invalid Git repository URL: {value}")
    if parsed.query or parsed.fragment:
        fail("Git repository URLs cannot contain a query or fragment; use --ref")
    if hostname == "github.com" and len(parsed.path.strip("/").split("/")) != 2:
        fail("use a GitHub repository root URL and --ref/--skill for selection")


def is_git_url(value: str) -> bool:
    return (
        value.startswith(("http://", "https://", "ssh://"))
        or value.startswith("git@")
        or (":" in value and "/" in value and not value.startswith(("/", ".")))
    )


def source_url_path_parts(url: str) -> list[str]:
    validate_git_url(url, allow_local=True)
    raw = url.rstrip("/")
    if "://" in raw:
        path = urlparse(raw).path
    elif ":" in raw and "/" in raw:
        path = raw.rsplit(":", 1)[1]
    else:
        path = raw

    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    return parts


def infer_source_name_from_url(url: str) -> str:
    parts = source_url_path_parts(url)
    if not parts or not parts[-1]:
        fail(f"could not infer source name from URL: {url}")
    return parts[-1]


def infer_owner_source_name_from_url(url: str) -> str | None:
    parts = source_url_path_parts(url)
    if len(parts) < 2 or not parts[-2] or not parts[-1]:
        return None
    return "/".join(parts[-2:])


def resolve_source_add_args(
    source: str, *, name: str | None, url: str | None
) -> tuple[str, str | None]:
    if is_git_url(source):
        if url:
            fail("source add URL positional cannot be combined with --url")
        return (name or infer_source_name_from_url(source), source)
    if name:
        fail("--name can only be used when the positional source is a URL")
    return (source, url)


def validate_source_name(name: str) -> None:
    if (
        name == "workspace"
        or not name
        or any(
            part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
            for part in name.split("/")
        )
    ):
        fail(f"unsafe or reserved source name: {name!r}")


def build_source_entry(
    *,
    name: str,
    url: str | None,
    path: str | None,
    ref: str | None,
    remote_name: str | None,
) -> tuple[str, dict]:
    validate_source_name(name)
    if not url and not path:
        fail("source add requires --url or --path")
    if not url and (ref or remote_name):
        fail("--ref and --remote-name require --url")

    entry: dict[str, Any] = {}
    if path:
        entry["path"] = path
    if url:
        validate_git_url(url, allow_local=True)
        remote: dict[str, str] = {"url": url}
        if remote_name:
            remote["name"] = remote_name
        if ref:
            remote["ref"] = ref
        entry["remote"] = remote
    return (name, entry)


def add_source_entry(registry: dict, name: str, entry: dict) -> None:
    if raw_source_by_name(registry, name) is not None:
        fail(f"source already exists: {name}")
    registry.setdefault("source", {})[name] = entry


def remove_source_entry(registry: dict, name: str) -> dict:
    sources = registry.get("source", {})
    if name in sources:
        return sources.pop(name)
    fail(f"unknown source: {name}")


def select_sources(sources: dict[str, Source], names: list[str]) -> list[Source]:
    selected = []
    seen = set()
    for name in names:
        source = sources.get(name)
        if source is None:
            fail(f"unknown source: {name}")
        if name not in seen:
            selected.append(source)
            seen.add(name)
    return selected


def depth_args(depth: int | None) -> list[str]:
    if depth is None:
        return []
    return ["--depth", str(depth)]


def deepen_args(depth: int) -> list[str]:
    return ["--deepen", str(depth)]


def remote_tracking_ref(remote: Remote) -> str:
    return f"refs/remotes/{remote.name}/{remote.ref}"


def remote_tracking_name(remote: Remote) -> str:
    return f"{remote.name}/{remote.ref}"


def remote_tracking_refspec(remote: Remote) -> str:
    return f"+{remote.ref}:{remote_tracking_ref(remote)}"


def git_network_label(cmd: list[str]) -> str:
    if len(cmd) >= 2 and cmd[0] == "git":
        return f"git {cmd[1]}"
    return cmd[0]


def run_git_network(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> None:
    for attempt in range(GIT_NETWORK_RETRIES + 1):
        try:
            run(cmd, cwd=cwd, dry_run=dry_run, progress=progress)
            return
        except subprocess.CalledProcessError:
            if attempt == GIT_NETWORK_RETRIES:
                raise
            retry = attempt + 1
            emit_event(
                progress,
                f"retry {retry}/{GIT_NETWORK_RETRIES} after {git_network_label(cmd)} failed",
            )
            time.sleep(GIT_NETWORK_RETRY_DELAY_SECONDS * (2**attempt))


def is_shallow_repository(cwd: Path) -> bool:
    result = git_process(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=cwd,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return git_ok(["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=cwd)


def has_merge_base(cwd: Path, left: str, right: str) -> bool:
    result = git_process(
        ["git", "merge-base", left, right],
        cwd=cwd,
    )
    return result.returncode == 0


def git_output(cmd: list[str], *, cwd: Path) -> str:
    result = git_process(
        cmd,
        cwd=cwd,
        check=True,
    )
    return result.stdout.strip()


def fetch_remote_branch(
    source: Source,
    remote: Remote,
    *,
    dry_run: bool,
    depth: int | None,
    progress: Progress | None = None,
) -> None:
    run_git_network(
        [
            "git",
            "fetch",
            *depth_args(depth),
            remote.name,
            remote_tracking_refspec(remote),
        ],
        cwd=source.path,
        dry_run=dry_run,
        progress=progress,
    )


def deepen_remote_branch(
    source: Source, remote: Remote, depth: int, *, progress: Progress | None = None
) -> None:
    run_git_network(
        [
            "git",
            "fetch",
            *deepen_args(depth),
            remote.name,
            remote_tracking_refspec(remote),
        ],
        cwd=source.path,
        dry_run=False,
        progress=progress,
    )


def ensure_fast_forwardable(
    source: Source, remote: Remote, *, progress: Progress | None = None
) -> None:
    upstream = remote_tracking_ref(remote)
    upstream_name = remote_tracking_name(remote)
    if is_ancestor(source.path, "HEAD", upstream):
        return

    if is_shallow_repository(source.path):
        for deepen_step in GIT_SHALLOW_DEEPEN_STEPS:
            deepen_remote_branch(source, remote, deepen_step, progress=progress)
            if is_ancestor(source.path, "HEAD", upstream):
                return

    if has_merge_base(source.path, "HEAD", upstream):
        raise SourceCannotFastForwardError(
            f"cannot fast-forward {remote.ref}: local checkout has commits not in {upstream_name}"
        )
    if is_shallow_repository(source.path):
        raise SourceCannotFastForwardError(
            f"cannot fast-forward {remote.ref}: shallow checkout still lacks common history with {upstream_name}"
        )
    raise SourceCannotFastForwardError(
        f"cannot fast-forward {remote.ref}: no common history with {upstream_name}"
    )


def reanchor_source_branch(
    source: Source, remote: Remote, *, progress: Progress | None = None
) -> None:
    status = git_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=source.path,
    )
    if status:
        raise SourceSyncError(
            f"cannot reanchor {remote.ref}: checkout has staged, tracked, or untracked changes"
        )

    upstream = remote_tracking_name(remote)
    old_head = git_output(["git", "rev-parse", "HEAD"], cwd=source.path)
    new_head = git_output(["git", "rev-parse", upstream], cwd=source.path)
    run(
        ["git", "checkout", "--no-overwrite-ignore", "-B", remote.ref, upstream],
        cwd=source.path,
        dry_run=False,
        progress=progress,
    )
    actual_head = git_output(["git", "rev-parse", "HEAD"], cwd=source.path)
    if actual_head != new_head:
        raise SourceSyncError(
            f"reanchor {remote.ref} did not reach {upstream}: expected {new_head}, got {actual_head}"
        )
    emit_event(
        progress,
        f"reanchor source {source.name}: {remote.ref} {old_head} -> {upstream} {new_head}",
    )


def sync_source(
    source: Source,
    *,
    dry_run: bool,
    depth: int | None = None,
    reanchor: bool = False,
    progress: Progress | None = None,
) -> None:
    remote = source.remote
    if source.path.exists():
        if remote is None:
            if not source.path.is_dir():
                fail(f"local source is not a directory: {source.path}")
            return
        if not git_ok(["git", "rev-parse", "--is-inside-work-tree"], cwd=source.path):
            fail(f"remote-bound source exists but is not a git repo: {source.path}")
        current_url = git_process(
            ["git", "remote", "get-url", remote.name],
            cwd=source.path,
        )
        if current_url.returncode != 0:
            run(
                ["git", "remote", "add", remote.name, remote.url],
                cwd=source.path,
                dry_run=dry_run,
                progress=progress,
            )
        elif current_url.stdout.strip() != remote.url:
            run(
                ["git", "remote", "set-url", remote.name, remote.url],
                cwd=source.path,
                dry_run=dry_run,
                progress=progress,
            )
        fetch_remote_branch(
            source, remote, dry_run=dry_run, depth=depth, progress=progress
        )
        if not dry_run:
            remote_branch = remote_tracking_ref(remote)
            local_branch = f"refs/heads/{remote.ref}"
            if git_ok(["git", "rev-parse", "--verify", local_branch], cwd=source.path):
                run(
                    ["git", "checkout", remote.ref],
                    cwd=source.path,
                    dry_run=False,
                    progress=progress,
                )
                try:
                    ensure_fast_forwardable(source, remote, progress=progress)
                except SourceCannotFastForwardError:
                    if not reanchor:
                        raise
                    reanchor_source_branch(source, remote, progress=progress)
                else:
                    run(
                        ["git", "merge", "--ff-only", remote_tracking_name(remote)],
                        cwd=source.path,
                        dry_run=False,
                        progress=progress,
                    )
            elif git_ok(
                ["git", "rev-parse", "--verify", remote_branch], cwd=source.path
            ):
                run(
                    [
                        "git",
                        "checkout",
                        "-b",
                        remote.ref,
                        "--track",
                        f"{remote.name}/{remote.ref}",
                    ],
                    cwd=source.path,
                    dry_run=False,
                    progress=progress,
                )
            else:
                run(
                    ["git", "checkout", remote.ref],
                    cwd=source.path,
                    dry_run=False,
                    progress=progress,
                )
        else:
            run(
                ["git", "checkout", remote.ref],
                cwd=source.path,
                dry_run=True,
                progress=progress,
            )
            if reanchor:
                emit_event(
                    progress,
                    f"Would reanchor source {source.name} to {remote_tracking_name(remote)} "
                    "only if the fetched history cannot fast-forward and the checkout is clean",
                )
        return

    if remote is None:
        fail(f"local source path does not exist: {source.path}")
    run_git_network(
        [
            "git",
            "clone",
            "--origin",
            remote.name,
            "--branch",
            remote.ref,
            *depth_args(depth),
            remote.url,
            str(source.path),
        ],
        dry_run=dry_run,
        progress=progress,
    )


def require_source_path(source: Source) -> None:
    if not source.path.exists():
        if source.remote is None:
            fail(f"local source path does not exist: {source.path}")
        raise SourceNotReadyError(
            f"source path does not exist: {source.path}", (source.name,)
        )
    if not source.path.is_dir():
        fail(f"source path is not a directory: {source.path}")
