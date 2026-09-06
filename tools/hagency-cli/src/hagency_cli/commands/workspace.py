from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import complete_directory
from hagency_cli.commands.shared import command_errors, render_event
from hagency_cli.workspace.discovery import init_workspace


def init_workspace_command(*, root: str | None, force: bool, dry_run: bool) -> None:
    init_workspace(
        root, Path.cwd(), force=force, dry_run=dry_run, progress=render_event
    )


@command_errors
def init_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Directory to initialize; defaults to the invocation directory",
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
    """Initialize a Hagency workspace with hagency-config.toml.

    Creates the invocation directory or --root if needed. This command does not
    search parent directories or fall back to the editable-installed checkout.
    The generated defaults use ~/Projects/references and shallow Git depth 1.

    An existing config requires --force, which replaces it rather than merging.
    --dry-run prints the path and configuration without writing or connecting.

    \b
    Examples:
      hgc init --root ./kit --dry-run
      hgc init --root ./kit
    """
    init_workspace_command(root=root, force=force, dry_run=dry_run)
