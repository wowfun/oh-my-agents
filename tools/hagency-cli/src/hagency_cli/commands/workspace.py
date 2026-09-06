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
