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

profile_app = make_app(
    help_text="""Manage reusable skill selections in profiles/NAME/config.toml.

    Add or update a profile, sync its sources, then apply it to a skills container.
    Editing a profile changes its configuration; apply materializes its skills.
    Commands resolve --root, then current-directory ancestors, then the
    editable-installed Hagency Kit checkout.

    Source names select their discovered skills. SOURCE:selector identifies a
    source-relative skill or subtree; a unique discovered skill name is also
    accepted. Use hgc skill list to inspect names and selectors.

    \b
    Examples:
      hgc profile add dev -AS workspace:skills/analyze-diff
      hgc source sync --profile dev
      hgc profile apply dev --dir ./project --dry-run
      hgc profile update --help
    """,
    add_completion=False,
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


@profile_app.command("ls", short_help="Alias for list.")
@profile_app.command("list", short_help="List profiles.")
@command_errors
def profile_list_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    """List workspace profiles with descriptions and source selections.

    Requires a Hagency workspace. Reads profiles/NAME/config.toml and prints name,
    description, and skills columns. The skills column lists selected source names,
    not individual installed skills. Use hgc skill list --profile NAME to discover
    that profile's skills. This command does not sync or apply profiles.

    \b
    Examples:
      hgc profile list --root ./kit
    """
    profile_list_command(root_value=root)


@profile_app.command("add", short_help="Add a profile.")
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Create profiles/NAME/config.toml in a Hagency workspace.

    The profile name must be new. An omitted --add-skill creates an empty profile.
    -AS/--add-skill accepts a source name, unique discovered skill name, or
    SOURCE:selector. Selectors are source-relative skills or subtrees; inspect
    them with hgc skill list. Obtain missing checkouts with hgc source sync first.

    Repeat --include/-i and --exclude/-e to select or exclude source-relative
    skills/subtrees; both require --add-skill. A source without includes selects
    all its discovered skills. An exact skill reference supplies an include
    selector automatically. --dry-run prints the proposed TOML without writes.
    Creating a profile does not install skills; use hgc profile apply afterward.

    \b
    Examples:
      hgc profile add dev --description 'Development skills'
      hgc profile add review -AS workspace -i skills/analyze-diff --dry-run
      hgc profile add review -AS workspace:skills/analyze-diff
    """
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


@profile_app.command("u", short_help="Alias for update.")
@profile_app.command("update", short_help="Update a profile.")
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Update a saved profile's description or skill selections.

    Requires an existing profile in a Hagency workspace. -AS/--add-skill accepts
    a source, unique skill name, or SOURCE:selector and merges that entry by
    default. --replace rewrites that entry's selection instead. -RS/--remove-skill
    removes a source entry or an exact included selector. Add and remove are
    mutually exclusive. Inspect candidates with hgc skill list first.

    Repeat --include/-i and --exclude/-e for source-relative skills/subtrees.
    These options and --replace require --add-skill. Missing source content must
    be obtained with hgc source sync before selector validation.
    --dry-run prints updated TOML without writes. Existing installations are
    unchanged; apply the profile separately to materialize its current selection.

    \b
    Examples:
      hgc profile update dev -AS workspace -i skills/analyze-diff --dry-run
      hgc profile update dev -AS workspace:skills/analyze-diff --replace
      hgc profile update dev -RS workspace
      hgc profile update dev --description 'Development and review skills'
    """
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


@profile_app.command("rm", short_help="Alias for remove.")
@profile_app.command("remove", short_help="Remove a profile.")
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned actions without changing files"),
    ] = False,
) -> None:
    """Remove a profile directory from the Hagency workspace.

    Requires an existing profiles/NAME directory. Removes that directory and its
    contents, including its config; installed skills and source checkouts remain
    in place. --dry-run prints the planned removal without changing files.

    \b
    Examples:
      hgc profile remove dev --dry-run
    """
    remove_profile(name=name, root_value=root, dry_run=dry_run, progress=render_event)


@profile_app.command("show", short_help="Show one profile config.")
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
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
) -> None:
    """Print one workspace profile configuration as TOML.

    Requires profiles/NAME/config.toml in the resolved Hagency workspace. Shows
    saved source/include/exclude selections without resolving or installing their
    skills. Use hgc skill list --profile NAME to inspect discovered candidates.

    \b
    Examples:
      hgc profile show dev --root ./kit
    """
    profile_show_command(name=name, root_value=root)


@profile_app.command(
    "apply",
    short_help="Apply profile skills into a target directory.",
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
    """Materialize a profile's selected skills in a target directory.

    Requires an existing profile and locally available source checkouts; obtain
    missing sources with hgc source sync --profile NAME. Choose exactly one of
    --path (the final skills container) or --dir (a project whose destination is
    DIR/.agents/skills). Relative destinations use the invocation directory and
    ~ expands to your home. --root and --checkout-dir only locate profile/sources.

    The default is symlink, or junction on Windows. -cp or --link-mode copy creates
    independent copies; -cp cannot be combined with symlink or junction modes.
    Explicit Windows symlinks may require an elevated terminal. Existing links of
    the selected type can be retargeted; independent directories are not overwritten.
    Unselected installations remain in place; there is no tracking or pruning.

    Duplicate skill names prompt for a source path in a TTY without changing the
    profile. Non-TTY use fails with guidance to narrow its include selectors.
    --dry-run previews materialization without writes and lists conflicts without
    prompting, even in a TTY. Installation is not transactional.

    \b
    Examples:
      hgc profile apply dev --dir ./project --dry-run
      hgc profile apply dev --path ./custom/skills --root ./kit
      hgc profile apply dev --dir ./project -cp
    """
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
