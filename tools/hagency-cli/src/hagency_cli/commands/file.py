from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import complete_directory
from hagency_cli.commands.purge_render import (
    render_paths_edit_report,
    render_purge_report,
)
from hagency_cli.commands.shared import command_errors, die, make_app
from hagency_cli.files.purge.models import PurgeRequest
from hagency_cli.files.purge.operations import purge_space
from hagency_cli.files.purge.roots import edit_purge_paths
from hagency_cli.files.sync.bundle.apply import apply_sync_bundle
from hagency_cli.files.sync.bundle.pack import pack_sync_bundle
from hagency_cli.files.sync.config import initialize_sftp_config
from hagency_cli.files.sync.models import (
    FileSyncConfigError,
    FileSyncError,
    FileSyncUsageError,
    SyncDirection,
)
from hagency_cli.files.sync.operations import sync_workspace_files
from hagency_cli.paths import expand_path

file_app = make_app(
    help_text="Manage project files and rebuildable artifacts.", add_completion=False
)

SFTPProjectRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Project/config root, or local root for a temporary endpoint",
        autocompletion=complete_directory,
    ),
]


SFTPRemoteArgument = Annotated[
    str | None,
    typer.Argument(
        help="Optional temporary [user@]host:path SFTP endpoint",
        metavar="REMOTE",
    ),
]


SFTPProfileOption = Annotated[
    str | None,
    typer.Option(
        "--profile",
        "-p",
        help=(
            "Named config or SFTP profile; use CONFIG: for the base config "
            "or CONFIG:PROFILE for a nested profile"
        ),
    ),
]


SFTPSyncDryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Compare both sides without changing any files"),
]


SFTPGitChangedOption = Annotated[
    bool,
    typer.Option(
        "--git-changed",
        help="Only upload paths changed in the local Git working tree",
    ),
]


SFTPPortOption = Annotated[
    int | None,
    typer.Option(
        "--port",
        "-P",
        min=1,
        max=65535,
        help="SSH port (temporary endpoint only)",
    ),
]


SFTPIdentityOption = Annotated[
    str | None,
    typer.Option(
        "--identity",
        "-i",
        help="SSH private key file (temporary endpoint only)",
    ),
]


SFTPExcludeOption = Annotated[
    list[str] | None,
    typer.Option(
        "--exclude",
        help="Gitignore-style pattern; repeatable (temporary endpoint only)",
    ),
]


SFTPSkipCreateOption = Annotated[
    bool,
    typer.Option(
        "--skip-create",
        help="Do not copy source-only paths (temporary endpoint only)",
    ),
]


SFTPIgnoreExistingOption = Annotated[
    bool,
    typer.Option(
        "--ignore-existing",
        help="Do not replace existing paths (temporary endpoint only)",
    ),
]


SFTPDeleteOption = Annotated[
    bool,
    typer.Option(
        "--delete",
        help="Delete destination-only paths (temporary endpoint only)",
    ),
]


SFTPUpdateOption = Annotated[
    bool,
    typer.Option(
        "--update",
        help="Do not replace newer destination paths (temporary endpoint only)",
    ),
]


SyncBundlePackRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Source project root",
        autocompletion=complete_directory,
    ),
]


SyncBundleApplyRootOption = Annotated[
    str | None,
    typer.Option(
        "--root",
        "-r",
        help="Destination root",
        autocompletion=complete_directory,
    ),
]


SyncBundleExcludeOption = Annotated[
    list[str] | None,
    typer.Option(
        "--exclude",
        help="Additional Gitignore-style source pattern; repeatable",
    ),
]


SyncBundleDryRunOption = Annotated[
    bool,
    typer.Option("--dry-run", help="Build and print the plan without writing files"),
]


SyncBundleDeleteOption = Annotated[
    bool,
    typer.Option("--delete", help="Apply authorized destination deletions"),
]


SyncBundleSkipCreateOption = Annotated[
    bool,
    typer.Option("--skip-create", help="Do not create paths missing at destination"),
]


SyncBundleIgnoreExistingOption = Annotated[
    bool,
    typer.Option("--ignore-existing", help="Do not replace existing paths"),
]


SyncBundleUpdateOption = Annotated[
    bool,
    typer.Option("--update", help="Do not replace newer destination paths"),
]


SyncBundleGitChangedOption = Annotated[
    bool,
    typer.Option(
        "--git-changed",
        help="Only pack paths changed in the local Git working tree",
    ),
]


def sync_files_command(
    *,
    direction: SyncDirection,
    root_value: str | None,
    profile: str | None,
    remote_endpoint: str | None,
    port: int | None,
    identity: str | None,
    exclude: list[str] | None,
    delete: bool,
    skip_create: bool,
    ignore_existing: bool,
    update: bool,
    git_changed: bool,
    dry_run: bool,
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        report = sync_workspace_files(
            root,
            direction,
            profile=profile,
            git_changed=git_changed,
            dry_run=dry_run,
            progress=print,
            remote_endpoint=remote_endpoint,
            port=port,
            identity=expand_path(identity, Path.cwd()) if identity else None,
            exclude=exclude or (),
            delete=delete,
            skip_create=skip_create,
            ignore_existing=ignore_existing,
            update=update,
        )
    except FileSyncUsageError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))

    action_count = len(report.actions)
    if action_count == 0:
        print("already in sync")
    elif dry_run:
        print(f"sync plan: {action_count} action(s)")
    else:
        print(f"sync complete: {action_count} planned action(s)")


def init_sftp_config_command(
    *, root_value: str | None, force: bool, dry_run: bool
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        initialize_sftp_config(
            root,
            force=force,
            dry_run=dry_run,
            progress=print,
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


def pack_sync_bundle_command(
    *,
    root_value: str | None,
    profile: str | None,
    no_config: bool,
    output: str | None,
    force: bool,
    git_changed: bool,
    exclude: list[str] | None,
    dry_run: bool,
) -> None:
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    output_path = expand_path(output, Path.cwd()) if output else None
    try:
        pack_sync_bundle(
            root,
            profile=profile,
            no_config=no_config,
            output_path=output_path,
            force=force,
            git_changed=git_changed,
            exclude=exclude or (),
            dry_run=dry_run,
            progress=print,
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


def apply_sync_bundle_command(
    *,
    bundle_value: str,
    root_value: str | None,
    delete: bool,
    skip_create: bool,
    ignore_existing: bool,
    update: bool,
    dry_run: bool,
) -> None:
    bundle_path = expand_path(bundle_value, Path.cwd())
    root = expand_path(root_value, Path.cwd()) if root_value else Path.cwd()
    try:
        apply_sync_bundle(
            bundle_path,
            root,
            delete=delete,
            skip_create=skip_create,
            ignore_existing=ignore_existing,
            update=update,
            dry_run=dry_run,
            progress=print,
        )
    except (FileSyncConfigError, FileSyncError, OSError) as exc:
        die(str(exc))


@file_app.command("push", help="Sync local project files to the remote.")
@command_errors
def local_to_remote_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    delete: SFTPDeleteOption = False,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    update: SFTPUpdateOption = False,
    git_changed: SFTPGitChangedOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    sync_files_command(
        direction=SyncDirection.LOCAL_TO_REMOTE,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        git_changed=git_changed,
        dry_run=dry_run,
    )


@file_app.command("pull", help="Sync remote project files to local.")
@command_errors
def remote_to_local_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    delete: SFTPDeleteOption = False,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    update: SFTPUpdateOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    sync_files_command(
        direction=SyncDirection.REMOTE_TO_LOCAL,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        git_changed=False,
        dry_run=dry_run,
    )


@file_app.command("sync", help="Synchronize local and remote project files over SFTP.")
@command_errors
def bidirectional_sync_cli(
    remote_endpoint: SFTPRemoteArgument = None,
    root: SFTPProjectRootOption = None,
    profile: SFTPProfileOption = None,
    port: SFTPPortOption = None,
    identity: SFTPIdentityOption = None,
    exclude: SFTPExcludeOption = None,
    skip_create: SFTPSkipCreateOption = False,
    ignore_existing: SFTPIgnoreExistingOption = False,
    dry_run: SFTPSyncDryRunOption = False,
) -> None:
    sync_files_command(
        direction=SyncDirection.BOTH,
        remote_endpoint=remote_endpoint,
        root_value=root,
        profile=profile,
        port=port,
        identity=identity,
        exclude=exclude,
        delete=False,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=False,
        git_changed=False,
        dry_run=dry_run,
    )


@file_app.command("init", help="Initialize .vscode/sftp.json in a project directory.")
@command_errors
def init_sftp_config_cli(
    root: SFTPProjectRootOption = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing SFTP config")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the config without changing files"),
    ] = False,
) -> None:
    init_sftp_config_command(root_value=root, force=force, dry_run=dry_run)


@file_app.command("pack", help="Create a portable offline sync ZIP from local files.")
@command_errors
def pack_sync_bundle_cli(
    root: SyncBundlePackRootOption = None,
    profile: SFTPProfileOption = None,
    no_config: Annotated[
        bool,
        typer.Option(
            "--no-config",
            help="Do not discover or read .vscode/sftp.json",
        ),
    ] = False,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            help="Output ZIP path (default: ./hgc-sync.zip)",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing output ZIP"),
    ] = False,
    git_changed: SyncBundleGitChangedOption = False,
    exclude: SyncBundleExcludeOption = None,
    dry_run: SyncBundleDryRunOption = False,
) -> None:
    pack_sync_bundle_command(
        root_value=root,
        profile=profile,
        no_config=no_config,
        output=output,
        force=force,
        git_changed=git_changed,
        exclude=exclude,
        dry_run=dry_run,
    )


@file_app.command("apply", help="Verify and apply a portable offline sync ZIP.")
@command_errors
def apply_sync_bundle_cli(
    bundle: Annotated[
        str,
        typer.Argument(help="Path to a sync ZIP created by hgc file pack"),
    ],
    root: SyncBundleApplyRootOption = None,
    delete: SyncBundleDeleteOption = False,
    skip_create: SyncBundleSkipCreateOption = False,
    ignore_existing: SyncBundleIgnoreExistingOption = False,
    update: SyncBundleUpdateOption = False,
    dry_run: SyncBundleDryRunOption = False,
) -> None:
    apply_sync_bundle_command(
        bundle_value=bundle,
        root_value=root,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        dry_run=dry_run,
    )


@file_app.command("purge", help="Find and remove rebuildable project artifacts.")
@command_errors
def space_purge_cli(
    paths: Annotated[
        list[str] | None,
        typer.Argument(
            help="Optional directories to scan instead of configured roots",
            metavar="PATH...",
            autocompletion=complete_directory,
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Preview selected removals without changing files"
        ),
    ] = False,
    edit_paths: Annotated[
        bool,
        typer.Option("--paths", help="Edit the configured purge scan directories"),
    ] = False,
) -> None:
    selected_paths = list(paths or [])
    if edit_paths and (selected_paths or dry_run):
        raise typer.BadParameter(
            "--paths cannot be combined with PATH or --dry-run", param_hint="--paths"
        )

    try:
        if edit_paths:
            report = edit_purge_paths()
            render_paths_edit_report(report)
        else:
            from hagency_cli.commands.purge_ui import QuestionaryPurgeUI

            request = PurgeRequest(
                paths=tuple(
                    Path(os.path.abspath(expand_path(value, Path.cwd())))
                    for value in selected_paths
                ),
                dry_run=dry_run,
            )
            report = purge_space(request, ui=QuestionaryPurgeUI())
            render_purge_report(report)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    except OSError as exc:
        die(f"could not complete file purge: {exc}")

    if report.exit_code:
        raise typer.Exit(report.exit_code)
