from __future__ import annotations

from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_profile,
    complete_profile_remove_reference,
    complete_selector,
    complete_skill_reference,
)
from hagency_cli.commands.shared import (
    command_errors,
    make_app,
    render_event,
    require_at_most_one,
    require_exactly_one,
)
from hagency_cli.workspace.config import render_toml
from hagency_cli.workspace.context import workspace_root_arg
from hagency_cli.workspace.operations.profiles import (
    add_profile,
    apply_profile_to_directory,
    remove_profile,
    update_profile,
)
from hagency_cli.workspace.profiles import (
    list_profile_configs,
    profile_skill_names,
    read_profile_config,
)
from hagency_cli.workspace.skills import LinkMode

profile_app = make_app(help_text="Manage profiles.", add_completion=False)


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


@profile_app.command("ls", help="Alias for list.")
@profile_app.command("list", help="List profiles.")
@command_errors
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
@command_errors
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
    add_profile(
        name=name,
        description=description,
        add_skill=add_skill,
        include=include,
        exclude=exclude,
        root_value=root,
        checkout_dir=None,
        dry_run=dry_run,
        progress=render_event,
    )


@profile_app.command("u", help="Alias for update.")
@profile_app.command("update", help="Update a profile.")
@command_errors
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
    update_profile(
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
        progress=render_event,
    )


@profile_app.command("rm", help="Alias for remove.")
@profile_app.command("remove", help="Remove a profile.")
@command_errors
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
    remove_profile(name=name, root_value=root, dry_run=dry_run, progress=render_event)


@profile_app.command("show", help="Show one profile config.")
@command_errors
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
    "apply",
    help="Apply profile skills into a target directory.",
    epilog="Migration: replace previous -p WORKSPACE usage with -d WORKSPACE.",
)
@command_errors
def profile_apply_cli(
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
    from hagency_cli.commands.skill_ui import QuestionarySkillConflictUI

    apply_profile_to_directory(
        conflict_ui=QuestionarySkillConflictUI(),
        name=name,
        skills_path=skills_path,
        skills_root=skills_root,
        copy=copy,
        link_mode=link_mode,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )
