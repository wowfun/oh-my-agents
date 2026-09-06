from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from hagency_cli.commands.file import file_app
from hagency_cli.commands.profile import profile_app
from hagency_cli.commands.service import service_app
from hagency_cli.commands.shared import make_app
from hagency_cli.commands.skill import skill_app
from hagency_cli.commands.source import source_app
from hagency_cli.commands.workspace import init_cli

app = make_app(
    help_text="Manage Hagency sources, skills, profiles, files, and services.",
    add_completion=True,
)
app.command("init", help="Initialize a Hagency workspace.")(init_cli)
app.add_typer(source_app, name="source")
app.add_typer(source_app, name="s", help="Alias for source.")
app.add_typer(skill_app, name="skill")
app.add_typer(profile_app, name="profile")
app.add_typer(profile_app, name="p", help="Alias for profile.")
app.add_typer(file_app, name="file")
app.add_typer(service_app, name="service")


def normalize_legacy_multi_value_options(args: Sequence[str]) -> list[str]:
    if (
        len(args) < 2
        or args[0] not in {"profile", "p"}
        or args[1] not in {"add", "update", "u"}
    ):
        return list(args)
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
