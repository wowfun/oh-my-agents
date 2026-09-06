from __future__ import annotations

from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_source,
)
from hagency_cli.commands.shared import command_errors, die, make_app, render_event
from hagency_cli.workspace.context import (
    load_registry,
    load_sources,
    workspace_root_arg,
)
from hagency_cli.workspace.operations.sources import (
    add_source,
    remove_source,
    sync_selected_sources,
)
from hagency_cli.workspace.sources import raw_source_by_name, resolve_sources

source_app = make_app(
    help_text="""Manage sources registered in hagency-config.toml.

    Add a local directory or Git repository, sync remote checkouts, then inspect
    their skills with hgc skill list. Removing a source edits the registry only.

    Each command accepts --root: explicit root, current-directory ancestors,
    then the editable-installed Hagency Kit checkout. --checkout-dir, where
    available, overrides defaults.checkout_dir_windows on native Windows or
    defaults.checkout_dir elsewhere. Windows falls back to checkout_dir.
    WSL uses the non-Windows setting; explicit source paths remain authoritative.

    \b
    Examples:
      hgc source add https://github.com/owner/repo.git --sync
      hgc source sync --profile dev --dry-run
      hgc source list
      hgc source sync --help
    """,
    add_completion=False,
)


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


@source_app.command("ls", short_help="Alias for list.")
@source_app.command("list", short_help="List configured sources.")
@command_errors
def source_list_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the platform checkout base; explicit source paths still win",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    """List configured sources and their resolved checkout paths.

    Requires a Hagency workspace. Output columns are name, type, path, url, and
    ref; local sources use '-' for remote fields. Missing checkouts can still be
    listed. --checkout-dir changes path resolution for this invocation only.
    This command does not fetch repositories or edit the registry.

    \b
    Examples:
      hgc source list --root ./kit
    """
    source_list_command(root_value=root, checkout_dir=checkout_dir)


@source_app.command("show", short_help="Show one configured source.")
@command_errors
def source_show_cli(
    name: Annotated[
        str, typer.Argument(help="Source name", autocompletion=complete_source)
    ],
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the platform checkout base; explicit source paths still win",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    """Show one configured source and its resolved path.

    Requires a registered source in a Hagency workspace. Prints local/remote
    settings without fetching the checkout or editing the configuration.
    --checkout-dir overrides checkout discovery for this invocation only.

    \b
    Examples:
      hgc source show my-skills --root ./kit
    """
    source_show_command(name=name, root_value=root, checkout_dir=checkout_dir)


@source_app.command(
    "add",
    short_help="Register a local or remote source.",
)
@command_errors
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Register a local directory or Git repository in hagency-config.toml.

    Requires an existing Hagency workspace. Pass a name with --path for a local
    source, a name with --url for a remote source, or a Git URL to infer its name.
    For a URL, --name overrides inference; name collisions try owner/repo before
    requiring an explicit name. --ref and --remote-name require a remote URL.
    Local relative paths resolve against the Hagency workspace root.

    Remote sources use configured checkout, remote name, ref, and depth defaults
    unless overridden. --path sets an explicit local or remote checkout path.
    --sync writes the registration, then obtains or updates the checkout; a sync
    failure leaves the registration in place. Without --sync, only the config
    changes. --dry-run previews registration and optional sync without fetching
    or writing files.

    \b
    Examples:
      hgc source add my-skills --path ./skills --dry-run
      hgc source add https://github.com/owner/repo.git --ref main --sync
      hgc source add my-skills --url https://github.com/owner/repo.git
    """
    add_source(
        source_value=source,
        name_value=name,
        url_value=url,
        path_value=path,
        ref_value=ref,
        remote_name=remote_name,
        sync=sync,
        root_value=root,
        dry_run=dry_run,
        progress=render_event,
    )


@source_app.command("rm", short_help="Alias for remove.")
@source_app.command("remove", short_help="Remove a source from the workspace config.")
@command_errors
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Remove a source entry from hagency-config.toml.

    Requires a registered source in a Hagency workspace. Refuses removal while
    profiles reference the source unless --force is supplied. Even with --force,
    only the registry entry is removed: checkouts, profile references, and installed
    skills remain in place. Those references may need manual profile updates.
    --dry-run previews the removal without writing the config.

    \b
    Examples:
      hgc source remove my-skills --dry-run
      hgc source remove my-skills --force
    """
    remove_source(
        name=name, force=force, root_value=root, dry_run=dry_run, progress=render_event
    )


@source_app.command("sync", short_help="Update persistent Git source checkouts.")
@command_errors
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    checkout_dir: Annotated[
        str | None,
        typer.Option(
            "--checkout-dir",
            help="Override the platform checkout base; explicit source paths still win",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Obtain or update persistent Git source checkouts.

    Requires a Hagency workspace and Git for remote sources. With no names or
    --profile, selects all configured sources. Names and --profile combine their
    source selections; local sources are checked without fetching. --slice applies after selection
    and uses 1-based indexes and inclusive ranges, such as 2:4 or 1,3:.

    --depth overrides defaults.depth. --checkout-dir overrides the platform's
    configured checkout base; explicit source paths remain authoritative. Normal
    updates require fast-forward history. --reanchor may discard local-only
    commits in selected checkouts when upstream cannot fast-forward; it requires
    no staged, unstaged, or untracked changes and creates no recovery refs.

    --dry-run prints intended Git operations without fetching or changing files;
    it cannot determine whether a later fetch will require reanchoring. Failures
    are reported per source, other sources continue, and the command exits with
    an error if any source fails. Rerun failed names or a slice to retry.

    \b
    Examples:
      hgc source sync --profile dev --depth 1 --dry-run
      hgc source sync --profile dev --slice 1,3:
      hgc source sync my-skills --reanchor
    """
    sync_selected_sources(
        names=list(names or []),
        profile_name=profile,
        depth=depth,
        source_slice=source_slice,
        reanchor=reanchor,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )
