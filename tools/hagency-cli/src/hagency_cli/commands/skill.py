from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import (
    complete_directory,
    complete_install_selector,
    complete_profile,
    complete_skill_add,
    complete_source_or_workspace,
)
from hagency_cli.commands.shared import (
    command_errors,
    make_app,
    render_event,
    require_at_most_one,
)
from hagency_cli.workspace.catalog import discover_catalog
from hagency_cli.workspace.context import load_sources, workspace_root_arg
from hagency_cli.workspace.operations.skills import add_skills
from hagency_cli.workspace.profiles import read_profile_config

skill_app = make_app(
    help_text="""Discover and install skills from SKILL.md directories.

    list scans available source content; add installs selected skills. A Hagency
    workspace must exist even when installing from a URL or local directory.
    Commands resolve --root, then current-directory ancestors, then the
    editable-installed Hagency Kit checkout. --checkout-dir affects source
    discovery; installation destinations resolve from the invocation directory.

    Sync sources first when you need updated content. Installation obtains only
    missing checkouts. Discovery is not an installed-skill inventory, and there
    is no uninstall or installation tracking.

    \b
    Examples:
      hgc skill list --source workspace
      hgc skill add workspace:skills/analyze-diff --dry-run
      hgc skill add owner/repo --all -d ./project
      hgc skill add --help
    """,
    add_completion=False,
)


def skill_list_command(
    *,
    source_filters: list[str],
    profile_name: str | None,
    root_value: str | None,
    checkout_dir: str | None,
) -> None:
    root = workspace_root_arg(root_value)
    sources = load_sources(root, checkout_dir)

    profile = read_profile_config(root, profile_name) if profile_name else None
    entries = discover_catalog(
        root,
        sources,
        profile=profile,
        source_filters=source_filters,
        progress=render_event,
    )
    print("source\tname\tselector\tpath")
    for entry in entries:
        print(
            "\t".join([entry.source_name, entry.name, entry.selector, str(entry.path)])
        )


@skill_app.command(
    "add",
    short_help="Install skills from a reference, source, Git URL, or local directory.",
)
@command_errors
def skill_add_cli(
    skill: Annotated[
        str,
        typer.Argument(
            help="Skill reference, source name, Git URL, owner/repo, or explicit local directory",
            autocompletion=complete_skill_add,
        ),
    ],
    selectors: Annotated[
        list[str] | None,
        typer.Option(
            "--skill",
            "-s",
            help="Source-relative skill selector; repeatable",
            autocompletion=complete_install_selector,
        ),
    ] = None,
    all_skills: Annotated[
        bool, typer.Option("--all", help="Select all discovered skills")
    ] = False,
    source_name: Annotated[
        str | None,
        typer.Option("--source-name", help="Name to register or reuse for a URL/path"),
    ] = None,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref", help="Remote branch or tag; existing checkouts are never switched"
        ),
    ] = None,
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
    global_install: Annotated[
        bool,
        typer.Option("--global", help="Install under ~/.agents/skills"),
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
    """Install skills from a reference, source, Git URL, or local directory.

    Requires an existing Hagency workspace; use hgc init first if needed. Inputs
    accept a unique discovered skill name, SOURCE:selector, registered source,
    Git URL, GitHub owner/repo, or explicit local path such as ./skills. Registered
    references take precedence over GitHub shorthand. Local inputs and destination
    paths resolve against the invocation directory; ~ expands to your home.

    The default destination is ./.agents/skills. Choose at most one of --path
    (exact container), --dir (DIR/.agents/skills), or --global (~/.agents/skills).
    --root and --checkout-dir locate sources, not the installation destination.
    Installation uses symlinks, or junctions on Windows, and refuses to overwrite
    independent directories. Existing links of the selected type may be retargeted.

    Repeat --skill for source-relative selectors or use --all; they are mutually
    exclusive and cannot accompany an exact skill input. Selectors must stay inside
    the source. One discovered skill installs directly; multiple skills need TTY
    multiselect or explicit selectors/--all in non-TTY use. Nothing is preselected.
    Duplicate install names still require a choice or narrower selectors, including
    with --all. All selections and conflicts resolve before target writes.

    URLs and local paths reuse matching sources or register new ones. Use
    --source-name to resolve ambiguous matches or choose a new registration name.
    Only missing checkouts are obtained; existing checkouts are not updated or
    switched. An explicit --ref mismatch requires source sync first. A new remote
    source persists its resolved --checkout-dir override; existing sources use it
    only for this invocation. Cancellation or fetch failure retains registration
    and any checkout. Installation is not transactional and has no tracking/pruning.

    --dry-run does not access the network or write configuration/targets. It may
    run read-only Git checks for an explicit ref. Missing checkouts yield a
    provisional report with unverified candidates. Conflict previews never prompt.

    \b
    Examples:
      hgc skill add workspace:skills/analyze-diff --dry-run
      hgc skill add owner/repo --skill one --skill nested/two -d ./project
      hgc skill add ./local-skills --all --global
      hgc skill add my-skills --all -p ./custom/skills -r ./kit
    """
    require_at_most_one({"--skill": selectors, "--all": all_skills})
    require_at_most_one(
        {"--path": skills_path, "--dir": skills_root, "--global": global_install}
    )
    from hagency_cli.commands.skill_ui import QuestionarySkillConflictUI

    add_skills(
        cwd=Path.cwd(),
        ui=QuestionarySkillConflictUI(),
        selectors=tuple(selectors or ()),
        all_skills=all_skills,
        source_name=source_name,
        ref=ref,
        skill=skill,
        skills_path=skills_path,
        skills_root=skills_root,
        global_install=global_install,
        root_value=root,
        checkout_dir=checkout_dir,
        dry_run=dry_run,
        progress=render_event,
    )


@skill_app.command("ls", short_help="Alias for list.")
@skill_app.command("list", short_help="List discovered skills.")
@command_errors
def skill_list_cli(
    sources: Annotated[
        list[str] | None,
        typer.Option(
            "--source",
            "-s",
            help="Limit to a source name or workspace",
            autocompletion=complete_source_or_workspace,
        ),
    ] = None,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Limit to skills selected by a profile",
            autocompletion=complete_profile,
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
) -> None:
    """List discoverable SKILL.md directories from local source content.

    Requires a Hagency workspace. With no filters, scans registered sources and
    the workspace. Repeat --source to narrow sources; workspace selects repo-local
    skills. --profile restricts discovery to that profile's selectors. Filters
    can be combined. Output columns are source, name, selector, and path.

    This is a discovery view, not an installed-skill inventory. It does not fetch
    missing checkouts; run hgc source sync to obtain or refresh remote sources.

    \b
    Examples:
      hgc skill list --source workspace
      hgc skill list --profile dev --root ./kit
    """
    skill_list_command(
        source_filters=list(sources or []),
        profile_name=profile,
        root_value=root,
        checkout_dir=checkout_dir,
    )
