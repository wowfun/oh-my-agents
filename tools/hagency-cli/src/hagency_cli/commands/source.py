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

source_app = make_app(help_text="Manage sources.", add_completion=False)


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


@source_app.command("ls", help="Alias for list.")
@source_app.command("list", help="List configured sources.")
@command_errors
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
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
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


@source_app.command("rm", help="Alias for remove.")
@source_app.command("remove", help="Remove a source from the workspace config.")
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
            help="Hagency workspace root",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    remove_source(
        name=name, force=force, root_value=root, dry_run=dry_run, progress=render_event
    )


@source_app.command("sync", help="Update persistent Git source checkouts.")
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
        progress=render_event,
    )
