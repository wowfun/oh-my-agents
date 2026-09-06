from __future__ import annotations

import inspect
import shlex
import sys
from functools import wraps
from typing import get_type_hints

import typer

from hagency_cli.workspace.errors import (
    SkillNameConflictError,
    SkillReferenceError,
    SkillSymlinkError,
    SourceBatchError,
    SourceNotReadyError,
    WorkspaceError,
)
from hagency_cli.workspace.events import OperationEvent


def die(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise typer.Exit(1)


def render_event(event: OperationEvent) -> None:
    print(event.message, file=sys.stderr if event.error else sys.stdout)


def command_errors(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except WorkspaceError as exc:
            values = inspect.signature(function).bind(*args, **kwargs).arguments
            die(render_workspace_error(exc, function.__name__, values))

    invoke.__annotations__ = get_type_hints(function, include_extras=True)
    return invoke


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


def make_app(*, help_text: str, add_completion: bool) -> typer.Typer:
    return typer.Typer(
        help=help_text,
        add_completion=add_completion,
        context_settings={"help_option_names": ["-h", "--help"]},
        rich_markup_mode=None,
        pretty_exceptions_enable=False,
        no_args_is_help=False,
    )


def render_workspace_error(error: WorkspaceError, command: str, values: dict) -> str:
    if isinstance(error, SkillNameConflictError):
        hint = (
            "use an exact --skill selector"
            if command == "skill_add_cli"
            else "narrow the profile's include selector"
        )
        return f"{error}; rerun in an interactive terminal to choose one or {hint}"
    if isinstance(error, SkillSymlinkError) and error.windows:
        hint = "; on Windows, rerun PowerShell or Git Bash as Administrator"
        if command == "profile_apply_cli":
            hint += ", use --link-mode junction, or use -cp"
        return str(error) + hint
    if isinstance(error, SkillReferenceError):
        if command == "skill_add_cli":
            prefix = ["hgc", "skill", "add"]
        else:
            action = "add" if command == "profile_add_cli" else "update"
            option = "-RS" if values.get("remove_skill") == error.reference else "-AS"
            prefix = ["hgc", "profile", action, values["name"], option]
        return (
            error.message
            + "\n"
            + "\n".join("  " + shlex.join([*prefix, ref]) for ref in error.references)
        )
    if isinstance(error, SourceNotReadyError):
        return (
            str(error)
            + "; run: "
            + shlex.join(["hgc", "source", "sync", *error.sources])
        )
    if isinstance(error, SourceBatchError) and error.reanchor:
        return (
            str(error)
            + "\nTip: if these checkouts are disposable and local-only commits may be discarded, run:\n  "
            + shlex.join(["hgc", "source", "sync", *error.reanchor, "--reanchor"])
        )
    return str(error)
