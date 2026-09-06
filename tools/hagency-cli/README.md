# Hagency CLI architecture

The CLI is organized around workspace content, project files, and the model proxy. It uses domain concepts to assign ownership, with operation functions composing the work. It does not require repositories, an event bus, or a dependency injection container.

| Package | Ownership |
| --- | --- |
| `cli.py` | Command tree, entrypoint, narrowly scoped legacy argument normalization |
| `commands` | Typer options, completion, terminal output, Questionary adapters, exit codes |
| `workspace/discovery.py` | Hagency root discovery and initialization |
| `workspace/config.py` | Hagency TOML encoding and persistence |
| `workspace/sources.py` | Source definitions, checkout resolution, Git synchronization |
| `workspace/skills.py` | Skill discovery, selectors, conflict resolution, symlink/copy/junction installation |
| `workspace/profiles.py` | Profile definitions, selection, application, source reference checks |
| `workspace/source_inputs.py` | Read-only classification and source reuse decisions for shortcut installation |
| `workspace/catalog.py` | Discoverable skill queries shared by commands and completion |
| `workspace/operations` | Source, profile, and skill workflows |
| `files/sync` | Configuration, selection/scanning, planning, content comparison, SFTP and offline bundles |
| `files/purge` | Scan roots, inspection/identity, scanning, safe deletion, workflow |
| `model_proxy` | Existing protocol, provider, hook, daemon and worker contracts |
| `paths.py` | Shared path expansion and platform normalization |

Dependencies flow from command adapters to operations and business modules. Profiles use skill discovery and installation; skills use source definitions. The source module does not read profiles: the source removal operation asks the profile module for references before removing a source. File sync and purge remain independent; neither depends on the Hagency workspace schema.

Workspace operations return results or reports and raise `WorkspaceError` subclasses. They emit optional `OperationEvent` callbacks rather than printing. Command adapters render progress and contextual retry commands. File operations retain their reports, error types, and progress callbacks. `RemoteFileSystem`, `PurgeUI`, and `SkillConflictUI` remain focused interfaces; shortcut multiselect extends the skill interaction contract with `SkillSelectionUI`.

Questionary imports happen when interactive adapters are used. SFTP imports Paramiko when connecting. The CLI can load lightweight proxy daemon/configuration code; ordinary commands and help do not import the HTTP server. The worker remains `hagency_cli.model_proxy.worker`, with unchanged hook contracts and service state locations. Python consumers should import the new owning modules directly; removed modules are not compatibility facades.

## Python import migration

This source refactor removes the old modules and exports. Import the owning module directly:

| Previous import | Current import |
| --- | --- |
| `hagency_cli.model_proxy.create_model_proxy_app`, `run_model_proxy` | `hagency_cli.model_proxy.server` |
| `hagency_cli.workspace.resolve_workspace_root` | `hagency_cli.workspace.discovery` |
| `hagency_cli.common.read_toml`, `write_toml` | `hagency_cli.workspace.config` |
| `hagency_cli.cli.parse_source_slice` | `hagency_cli.workspace.operations.sources` |
| `hagency_cli.profiles.install_skill`, `resolve_selector` | `hagency_cli.workspace.skills` |
| `hagency_cli.file_sync.sync_workspace_files` | `hagency_cli.files.sync.operations` |
| `hagency_cli.space.purge.purge_space` | `hagency_cli.files.purge.operations` |
| `hagency_cli.space.purge.PurgeRequest`, `PurgeReport`, `PurgeUI` | `hagency_cli.files.purge.models` |

Importing `model_proxy.server` explicitly loads the HTTP implementation; the worker already uses that path. Hook-facing imports such as `AuthPatch` and `HeaderPatch` stay in `hagency_cli.model_proxy`. Workspace APIs raise `hagency_cli.workspace.errors.WorkspaceError` instead of terminating the process. File operation errors retain their own types. The command layer translates failures to CLI exit codes.

## Three root policies

- Workspace commands resolve explicit `--root`, then current-directory ancestors, then the editable Hagency Kit source checkout. The fallback checks the exact source-tree layout, including the new `workspace/discovery.py` path. An ordinary wheel does not infer a workspace from the installation directory. `hgc init` always targets its explicit path or the invocation directory.
- File operations use the invocation directory or their project `--root`. Temporary SFTP endpoints bypass project SFTP configuration. Bundle commands retain their local project/destination rules.
- Purge uses explicit scan directories, saved `space-purge-paths`, or its own automatic roots. It does not use workspace discovery.

Installation destinations independently resolve against the invocation directory. `--path` is an exact skills container; `--dir` appends `.agents/skills`; skill installation also supports `--global` and defaults to the current directory's `.agents/skills`.

## Shortcut installation workflow

`workspace/source_inputs.py` classifies an input and resolves a source without mutation. Registered references precede GitHub shorthand. Explicit local paths use the most specific containing source and keep discovery inside the supplied subtree. Remote reuse compares URLs conservatively and respects explicit refs. New names and checkout paths are validated before registration.

`workspace/operations/skills.py` then registers a new source, obtains a missing checkout, resolves every selection and duplicate-name conflict, and installs. Existing checkouts are not fetched or switched. Explicit refs are checked using read-only Git commands, including for local paths inside remote checkouts. Selectors are validated for source containment before acquisition and again against the obtained content. A new remote source inherits ref/depth defaults; an explicit checkout override is persisted as its actual checkout path. Cancellation or acquisition failure retains completed registration and checkout work. Target installation is not transactional.

Dry runs do not access the network or write configuration/targets. A missing checkout yields a provisional report because its candidates and installation plan cannot be verified. Discoverable local candidates can be validated and previewed. Conflict previews never prompt, even in a TTY. Multiple skills require explicit filters or interactive selection; `--all` still resolves duplicate installation names.

Each source Git command has a five-minute deadline. A timeout stops acquisition or records a failed source sync and continues with other selected sources. Shortcut installation keeps completed registration and any acquired checkout when a Git command fails.

Explicit SFTP agents belong to individual connections; connecting does not modify `SSH_AUTH_SOCK`. Unix agent sockets use the connection timeout. Native Windows retains Paramiko's Pageant/OpenSSH discovery. The adapter's small integration with Paramiko's existing agent authentication is tested with Paramiko 3.5.1 and 4.0.0, including signing through a temporary local OpenSSH agent. Online sync preserves symlink targets, including absolute and dangling links, without scanning through them. Offline bundles continue to skip symlinks.

Purge's descriptor-based removal claims a unique temporary name for the emptied artifact, checks its identity while retaining the open descriptor, and removes that name. This preserves directories concurrently recreated at the original path. If the final identity check or removal fails, the report identifies the quarantine path where remaining contents are retained.

## Validation

From this directory:

```sh
uv run --frozen python -m unittest discover -s tests
uv run --frozen python -m unittest tests.test_review_regressions
uv build --out-dir /tmp/hagency-cli-dist
git diff --check
```

The suite includes CLI `main()` parsing and command removal, dependency and lazy-import boundaries, workspace discovery, local Git acquisition/update behavior, selection/cancellation, dry runs, installation modes, SFTP credential/Git protection, bundle validation before writes, purge revalidation, and proxy lifecycle coverage. Mocks target the module that owns each dependency.

Release validation also installs the wheel in an isolated environment outside a Hagency workspace and tests an isolated editable installation. Native filesystem behavior is established only for the host platform; mocked Windows/macOS branches do not establish native execution, and local/fake transports do not establish live SFTP or upstream provider behavior.
