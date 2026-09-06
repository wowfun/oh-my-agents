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
    help_text="""Manage project files and rebuildable artifacts.

    init creates a placeholder .vscode/sftp.json; edit it before syncing.
    push uploads, pull downloads, and sync transfers in both directions over
    SFTP. A temporary [user@]host:path endpoint bypasses that configuration.
    pack and apply transfer offline ZIP bundles without a network connection.
    purge finds rebuildable artifacts and confirms permanent deletion in a TTY.

    Sync and bundle commands use the invocation directory or their --root.
    They do not discover a Hagency workspace. Purge uses explicit paths, a saved
    path list, or automatic project roots. Online --dry-run still connects and
    reads both sides; offline dry runs read local data only.

    \b
    Examples:
      hgc file init --root ./project --dry-run
      hgc file push dev@server:/srv/project -r ./project --dry-run
      hgc file pack -r ./project -o transfer.zip
      hgc file apply transfer.zip -r ./restore --dry-run
      hgc file purge ./project --dry-run
      hgc file sync --help
    """,
    add_completion=False,
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


@file_app.command("push", short_help="Sync local project files to the remote.")
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
    """Upload local project files to an SFTP destination.

    Uses .vscode/sftp.json under the invocation directory or --root, including its
    context, remotePath, ignore rules, and syncOption settings. For multiple configs
    or profiles use --profile NAME, CONFIG:PROFILE, or CONFIG: for the base config.
    No Hagency workspace is required.

    Passing REMOTE bypasses all SFTP config, including malformed files, and uses
    --root or the invocation directory as the local tree. REMOTE must include a
    path: [user@]host:/path, host:~/path, host:., host:C:/path, or [IPv6]:/path.
    SSH aliases are supported. --profile cannot accompany REMOTE. --port,
    --identity, --exclude, --delete, --skip-create, --ignore-existing, and --update
    require REMOTE; in config mode set their equivalents in .vscode/sftp.json.

    The local side wins shared-path conflicts. Destination-only paths remain
    unless syncOption.delete or temporary --delete is enabled. --update preserves
    newer destinations. Temporary mode defaults to no deletion. All modes protect
    .git metadata and every .vscode/sftp.json, even against negated ignore rules.

    SSH host keys must already be trusted. Temporary mode uses SSH config, agents,
    or keys; load encrypted keys into your agent. Endpoint user, --port, and
    --identity override SSH config. A Windows SSH alias running RemoteCommand wsl
    still uses Windows SFTP; a WSL filesystem needs its own reachable SFTP server.

    --git-changed narrows the plan to staged, unstaged, untracked, deleted, and
    renamed Git paths. Git errors fail before connecting; a clean worktree returns
    without a connection. Deleting old rename paths still requires deletion enabled.
    Ordinary uploads need no Git. Same-size edits within one mtime second can be
    missed; --git-changed does not force comparison. CRLF/LF-equivalent text is left
    untouched, and transferred bytes are never rewritten. Counts are planned actions.

    --dry-run still connects and reads both sides, but writes or deletes nothing.
    Preview before enabling deletion.

    \b
    Examples:
      hgc file push --root ./project --dry-run
      hgc file push dev@server:/srv/project -r ./project --dry-run
      hgc file push server:/srv/project --git-changed --delete --dry-run
      hgc file push --profile staging
    """
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


@file_app.command("pull", short_help="Sync remote project files to local.")
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
    """Download SFTP files into the local project tree.

    Uses .vscode/sftp.json under the invocation directory or --root, mapping
    remotePath to local context. --profile selects NAME, CONFIG:PROFILE, or CONFIG:
    for the base config. No Hagency workspace is required.

    REMOTE bypasses all config and downloads into --root or the invocation directory.
    Use [user@]host:/path, host:~/path, host:., host:C:/path, or [IPv6]:/path;
    SSH aliases work and the remote path must be nonempty. REMOTE and --profile
    are mutually exclusive. --port, --identity, --exclude, --delete, --skip-create,
    --ignore-existing, and --update require REMOTE. Config mode reads the
    corresponding SSH, ignore, and syncOption settings from .vscode/sftp.json.

    The remote side wins shared-path conflicts. Local-only paths remain unless
    syncOption.delete or temporary --delete is enabled; --update preserves newer
    local files. Temporary mode defaults to no deletion. .git metadata and every
    .vscode/sftp.json are protected at all depths, including against negation rules.
    SSH host keys must already be trusted; temporary mode uses SSH config, agents,
    or keys. Endpoint user, --port, and --identity override SSH config; preload
    encrypted keys in your agent. A remote WSL tree needs an SFTP server inside WSL,
    not a Windows alias with RemoteCommand wsl.

    --dry-run still connects and reads both sides without writes/deletes. Shared
    CRLF/LF-equivalent text is left untouched and transferred bytes are preserved.
    Same-size edits within one mtime second can be missed; there is no force/checksum
    option. Counts report planned actions. Preview before enabling deletion.

    \b
    Examples:
      hgc file pull --root ./project --dry-run
      hgc file pull server:~/project --root ./restore --dry-run
      hgc file pull server:/srv/project --delete --dry-run
    """
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


@file_app.command(
    "sync", short_help="Synchronize local and remote project files over SFTP."
)
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
    """Synchronize local and remote files in both directions over SFTP.

    Uses .vscode/sftp.json under the invocation directory or --root, mapping local
    context to remotePath. --profile selects NAME, CONFIG:PROFILE, or CONFIG: for
    the base config. No Hagency workspace is required.

    REMOTE bypasses all config and uses --root or the invocation directory as the
    local tree. It requires a nonempty remote path: [user@]host:/path, host:~/path,
    host:., host:C:/path, or [IPv6]:/path. SSH aliases work. REMOTE and --profile
    are mutually exclusive. --port, --identity, --exclude, --skip-create, and
    --ignore-existing require REMOTE; config mode reads their equivalents from
    .vscode/sftp.json. SSH host keys must already be trusted. SSH config, agents,
    and keys supply authentication; endpoint user, --port, and --identity override
    SSH config. Preload encrypted keys in your agent. Remote WSL trees need an SFTP
    server inside WSL; RemoteCommand wsl on a Windows alias does not provide one.

    Unique files are copied to the other side. The precisely newer shared file
    wins, with local winning an exact timestamp tie. Only skipCreate and
    ignoreExisting affect bidirectional sync; --delete, --update, and --git-changed
    are not supported. .git metadata and every .vscode/sftp.json are protected,
    including from negated ignore rules.

    --dry-run still connects and reads both trees, without writes or deletes.
    CRLF/LF-equivalent text is left untouched; transfers preserve original bytes.
    Same-size edits within one mtime second can be missed. There is no force/checksum
    option, and summaries count planned actions.

    \b
    Examples:
      hgc file sync --root ./project --dry-run
      hgc file sync dev@server:/srv/project --exclude '*.tmp' --dry-run
      hgc file sync --profile staging
    """
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


@file_app.command(
    "init", short_help="Initialize .vscode/sftp.json in a project directory."
)
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
    """Create a placeholder .vscode/sftp.json in an existing project.

    Uses the invocation directory or --root directly, without Hagency workspace
    lookup. The project directory must already exist. Edit placeholder connection
    settings before using push, pull, or sync; initialization never connects.
    An existing config requires --force to overwrite it. --dry-run prints the
    config and planned path without creating files.

    \b
    Examples:
      hgc file init --root ./project --dry-run
      hgc file init --root ./project
    """
    init_sftp_config_command(root_value=root, force=force, dry_run=dry_run)


@file_app.command(
    "pack", short_help="Create a portable offline sync ZIP from local files."
)
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
    """Create a portable offline sync ZIP from local files.

    Reads the invocation directory or --root without Hagency workspace lookup.
    When .vscode/sftp.json exists, reuses only profile selection, context, ignore,
    and ignoreFile; connection/authentication settings are neither used nor stored.
    Missing config means ordinary-directory mode. --no-config bypasses even an
    invalid config and cannot accompany --profile. No network connection is made.

    By default packs a full snapshot to ./hgc-sync.zip in the invocation directory.
    Relative --output paths also use that directory; --force permits replacement.
    --git-changed packs a Git patch of staged, unstaged, untracked, deleted, and
    renamed paths, with deletion markers for removed/old rename paths. A clean or
    fully filtered Git selection creates no ZIP. Full snapshots need no Git.

    Always excludes .git metadata, every .vscode/sftp.json, and the output ZIP,
    even against negated ignore rules. Repeat --exclude for Gitignore patterns.
    Symlinks are skipped with warnings and recorded in the manifest. The manifest
    is capped at 16 MiB; total payload size is not capped. SHA-256 detects corruption
    but the ZIP is not authenticated or encrypted.

    --dry-run reads and hashes selected files without writing a ZIP. Transfer a
    real ZIP through your chosen channel, then run hgc file apply at the destination.
    Reverse transfer requires packing from the other side.

    \b
    Examples:
      hgc file pack --root ./project --dry-run
      hgc file pack -r ./project -o transfer.zip --no-config
      hgc file pack --git-changed --exclude '*.tmp' --force
    """
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


@file_app.command("apply", short_help="Verify and apply a portable offline sync ZIP.")
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
    """Verify and apply an offline ZIP created by hgc file pack.

    BUNDLE and --root resolve from the invocation directory; the destination
    defaults to that directory. Requires no Hagency workspace, SFTP config, Git,
    or network. A missing destination is created only during a real apply.

    Validates the whole archive, paths, entries, sizes, and SHA-256 hashes before
    any destination write. Rejects protected .git metadata, SFTP config paths,
    and excluded paths. Checksums detect corruption, not authenticity.

    The bundle wins shared-path conflicts. --skip-create preserves absent paths,
    --ignore-existing preserves existing ones, and --update preserves newer files.
    Without --delete, destination-only paths stay. For full snapshots, --delete
    mirrors non-ignored paths; for Git patches it applies only manifest deletion
    markers. Writes use atomic file replacement and deletions happen last; the
    whole apply is not transactional. CRLF/LF-equivalent text is left untouched,
    and transferred bytes are preserved.

    --dry-run verifies and previews without creating the destination or changing
    files. Preview before enabling deletion.

    \b
    Examples:
      hgc file apply transfer.zip --root ./restore --dry-run
      hgc file apply transfer.zip --root ./restore
      hgc file apply transfer.zip --root ./restore --delete --dry-run
    """
    apply_sync_bundle_command(
        bundle_value=bundle,
        root_value=root,
        delete=delete,
        skip_create=skip_create,
        ignore_existing=ignore_existing,
        update=update,
        dry_run=dry_run,
    )


@file_app.command("purge", short_help="Find and remove rebuildable project artifacts.")
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
    """Find and permanently remove rebuildable project artifacts.

    Scans dependency directories, build output, caches, and valid CACHEDIR.TAG
    directories. Explicit PATH values replace saved and automatic roots for this
    invocation. Otherwise a nonempty per-user space-purge-paths file wins; missing
    or empty files restore automatic home project roots and agent worktrees.
    Purge does not discover a Hagency workspace. A worktree location alone does
    not qualify a directory: candidates must match an artifact name or valid
    CACHEDIR.TAG and pass project and Git checks. Git-tracked artifacts and
    linked directories are excluded.

    --paths is an exclusive mode: create/edit the saved roots with VISUAL, EDITOR,
    or the platform editor, then reload. It cannot accompany PATH or --dry-run.
    Entries are absolute or ~ paths, one per line; blanks and # comments are ignored.
    The file lives under XDG_CONFIG_HOME/hagency or ~/.config/hagency on Linux/WSL,
    ~/Library/Application Support/Hagency on macOS, or APPDATA/Hagency on Windows
    (with ~/.config/hagency as fallback).

    In a TTY, select candidates; only artifacts inactive for strictly more than
    seven days are preselected. Permanent deletion then requires confirmation,
    which defaults to No. --dry-run still opens the selection UI but only previews.
    Non-TTY invocations always preview, even without --dry-run. Partial deletion
    may leave some contents removed; other selections continue and exit status 1
    indicates incomplete cleanup. Nothing is moved to Trash or the Recycle Bin.

    \b
    Examples:
      hgc file purge ./project --dry-run
      hgc file purge ./project
      hgc file purge --paths
    """
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
