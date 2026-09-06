# Hagency CLI

`hgc` manages Hagency workspaces, skill sources, skill installation, reusable profiles, project file transfers, artifact cleanup, and a local model proxy. Each command's `--help` includes its prerequisites, defaults, effects, and examples, including when no workspace is available.

- [Install and get started](#install-and-get-started)
- [Directories and configuration](#directories-and-configuration)
- [Sources](#sources)
- [Skills](#skills)
- [Profiles](#profiles)
- [SFTP file sync](#sftp-file-sync)
- [Offline sync bundles](#offline-sync-bundles)
- [Project artifact purge](#project-artifact-purge)
- [Local model proxy](#local-model-proxy)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Install and get started

Requires Python 3.11 or newer. From the Hagency Kit repository root:

```sh
uv tool install -e tools/hagency-cli
hgc --help
hgc skill list --source workspace
hgc skill add workspace:skills/analyze-diff --dry-run
hgc skill add workspace:skills/analyze-diff
```

The last command links the skill into the invocation directory's `.agents/skills`. Use `--dir /path/to/project` for another project's skills container, or `--global` for your user's `~/.agents/skills`. Inspect each command before running it:

```sh
hgc source --help
hgc source sync --help
hgc profile apply --help
hgc file push --help
hgc service model-proxy --help
```

The command tree is:

```text
hgc init
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|purge
hgc service model-proxy start|stop|restart
```

Help lists command aliases on the same row as their full names: `source, s`, `profile, p`, `list, ls`, `remove, rm`, and `update, u`. Both forms accept the same parameters and show the same full help. Option aliases likewise share a row, such as `-p, --path PATH`. `source sync` updates Git checkouts; `file sync` synchronizes project files over SFTP.

Install shell completion or print its script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It respects workspace discovery and `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted. Completion performs no network access or writes.

For a non-editable installation, use `uv tool install tools/hagency-cli`, or install a built wheel. Such installations require an explicit `--root` when run outside a Hagency workspace. The packaged help works without access to this repository or README.

## Directories and configuration

### Workspace discovery

Workspace commands resolve their Hagency root in this order:

1. Explicit `-r/--root`, which must contain `hagency-config.toml`.
2. The invocation directory and its ancestors, using the nearest `hagency-config.toml`.
3. The Hagency Kit source checkout supplying an editable installation, if its root config exists.

The editable fallback checks only that checkout's root. An ordinary wheel has no installation-directory fallback. `hgc init` always initializes its explicit `--root` or the invocation directory, creates that directory if necessary, and never uses workspace discovery:

```sh
hgc init --root ./kit --dry-run
hgc init --root ./kit
```

Initialization writes checkout and depth defaults. An existing config requires `--force`, which replaces it rather than merging. `--dry-run` prints the proposed configuration without writing.

File sync and bundle commands use the invocation directory or their project `--root` directly. They need no Hagency workspace. Purge uses its own [scan roots](#project-artifact-purge). The model proxy accepts an explicit `--config` without a workspace; otherwise its config is resolved under the Hagency root.

### Workspace and profile configuration

The source registry is `hagency-config.toml`. A minimal example with a local and a remote source:

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
depth = 1
remote_name = "origin"
remote_ref = "main"

[source.local-skills]
path = "./skills"

[source.example.remote]
url = "https://github.com/owner/repo.git"
ref = "main"
```

`[source.NAME].path` is an explicit local or checkout path; relative values resolve against the Hagency root. Without an explicit path, remote checkouts use `<checkout_dir>/<source-name>`. `--checkout-dir` overrides the checkout base, then native Windows uses `checkout_dir_windows` when present, then `checkout_dir` applies. WSL uses `checkout_dir`. Native Windows normalizes Git Bash paths such as `/d/Projects/references` to `D:/Projects/references`. There is no separate Windows CLI flag.

`remote_name` and `remote_ref` default to `origin` and `main`; per-source `remote.name` and `remote.ref` override them. `depth` is the default shallow sync depth, and `source sync --depth` overrides it. `hgc init` writes depth 1; omitting depth from an existing config leaves it unset. The built-in `workspace` skill source represents the Hagency root and does not need a registry entry.

Profiles live in `profiles/NAME/config.toml`. For example:

```toml
name = "review"
description = "Review changes"

[skill.workspace]
include = ["skills/analyze-diff"]
```

Each `[skill.SOURCE]` selects a source. Optional `include` and `exclude` lists contain source-relative skill or subtree selectors. Without includes, all discovered skills in the source are selected before exclusions. Use `hgc skill list` to inspect selectors; selectors must remain inside the source. Edit profiles through [profile commands](#profiles).

### Installation destinations

Destinations are independent of source discovery. Relative destinations resolve against the invocation directory and `~` expands to the current user's home.

| Option | Destination |
| --- | --- |
| `-p/--path PATH` | The exact skills container, with only each skill name appended |
| `-d/--dir DIR` | `DIR/.agents/skills` |
| `skill add --global` | `~/.agents/skills` |
| `skill add` without destination options | `./.agents/skills` |

`skill add` accepts at most one destination option; `profile apply` requires exactly one of `--path` or `--dir`. `--root` and `--checkout-dir` locate profiles and sources rather than choosing an installation target. A newly registered remote source persists the resolved `skill add --checkout-dir` override; existing sources use it for that invocation only.

## Sources

Register a directory or Git repository, inspect it, then sync its content:

```sh
hgc source add local-skills --path ./skills --dry-run
hgc source add https://github.com/owner/repo.git --ref main --sync
hgc source list
hgc source show repo
hgc source sync --profile dev --depth 1 --dry-run
hgc source sync --profile dev --depth 1
hgc source sync --profile dev --slice 1,3:
```

`source add` requires an existing workspace. Pass a name with `--path` for a local source, a name with `--url` for a remote source, or a Git URL to infer the name. If that name is occupied, URL inference tries `owner/repo`; `--name` overrides inference. `--ref` and `--remote-name` require a remote URL. `--sync` writes the registry first and then synchronizes; a sync failure retains the registration. `--dry-run` previews both stages without fetching or writing.

`source list` prints name, type, resolved path, URL, and ref. `source show NAME` prints one source's configured and resolved values. Both read local configuration without fetching missing checkouts.

With no names or `--profile`, sync selects all configured sources; explicit names and profile sources combine. Local source paths are checked without fetching. `--slice/-s` applies after selection using 1-based indexes and inclusive ranges such as `4:`, `2:4`, `:3`, or `1,3:`. Failures are reported per source while other selected sources continue. Retry failed names or slices; no resume state is saved. Transient Git network failures are retried automatically, with a five-minute deadline per Git command.

Normal sync refuses non-fast-forward updates. If upstream rewrites history and a checkout is disposable, `--reanchor` can replace local-only commits with fetched upstream history. It requires no staged, unstaged, or untracked changes and creates no recovery refs:

```sh
hgc source sync repo --reanchor --dry-run
hgc source sync repo --reanchor
```

Sync dry runs do not fetch or change files, so they describe reanchoring conditionally. Use reanchoring only when discarding local-only commits is intended.

`source remove NAME` removes only the registry entry, leaving the checkout and installed skills in place. Profile references block removal unless `--force` is supplied; forced removal leaves those references unchanged. Preview with `--dry-run`, and update profiles separately when removing a referenced source.

## Skills

`skill list` discovers `SKILL.md` directories in the workspace and registered source content. It does not track installed skills or fetch missing checkouts:

```sh
hgc skill list
hgc skill list --source workspace
hgc skill list --source repo --root ./kit
hgc skill list --profile dev
```

Repeat `--source` to narrow discovery; it can be combined with `--profile`. Output columns are source, name, selector, and path. Run `source sync` before discovery when remote content is missing or needs updating. Installation uses the [destination rules](#installation-destinations) above.

```sh
hgc skill add owner/repo --skill code-review --skill tools/testing
hgc skill add https://github.com/owner/repo.git --ref main --all
hgc skill add ./local-skills --all
hgc skill add existing-source --skill nested/one --dry-run
```

A resolvable Hagency workspace is required; installation never initializes one implicitly. Inputs accept a discovered skill name, exact `SOURCE:selector`, registered source name, Git URL, GitHub `owner/repo`, or explicit local directory (`./`, `../`, `~`, absolute and Windows paths). Registered sources and their selectors take precedence over GitHub shorthand. Local paths resolve from the invocation directory.

Repeat `--skill/-s` for source-relative selectors, or use `--all`; these options are mutually exclusive and cannot be added to an exact skill reference. Selectors must stay within the source: absolute paths, escaping `..` paths, and symlinks pointing outside are rejected. A source with one skill installs directly. Multiple skills without filters require terminal multiselect with nothing selected by default; non-interactive use requires selectors or `--all`. Cancellation or an empty selection installs nothing. All selections and duplicate-name conflicts are resolved before the first installation write, including with `--all`.

Remote inputs reuse the unique source matching the URL and any explicit ref. GitHub URLs normalize trailing `/` and `.git` within the same transport; other URLs match exactly, and HTTPS is not equated with SSH. Multiple matches require `--source-name`. A new remote source uses the repository name, then `owner/repo` on a name collision, then requires an explicit name. A local directory reuses the most specific containing source, falling back to the workspace, and limits discovery to the supplied subtree. Tied sources require a name; otherwise a new local source uses the directory name. New names cannot be `workspace` or contain path traversal or ambiguous references.

New remote sources use workspace default ref/depth. Only missing checkouts are fetched; existing URL/ref/branch settings and checkout contents are preserved. An explicit ref mismatch requires `source sync` first. An occupied, unregistered checkout path fails before registration. `--checkout-dir` is saved as the resolved checkout path for a new remote source; for existing sources it affects only the invocation's checkout discovery. After static validation, installation registers, obtains, selects, then installs. A fetch failure or cancellation retains the source registration and any checkout, with progress describing completed stages; installation does not promise rollback.

Skill installation `--dry-run` performs no network access or config/target writes. If the checkout is missing, it reports proposed registration/fetch steps and marks candidates and the installation plan as unverified. Existing checkouts use read-only Git commands to verify an explicit ref; a local subdirectory inside a remote checkout also accepts `--ref`. `skill list/ls` lists discoverable skills; it is not an installed-skill inventory.

Installation uses symlinks on non-Windows systems and junctions on Windows. Independent directories are not overwritten; existing links of the selected type may be retargeted. Unselected installations remain in place. Installation has no tracking or pruning and is not transactional.

## Profiles

Create or edit reusable skill selections, then apply them to a target container:

```sh
hgc profile add review --description "Review changes" -AS workspace:skills/analyze-diff
hgc profile update review -AS workspace -i skills/log-analyzer --dry-run
hgc profile update review -AS workspace -i skills/log-analyzer
hgc profile show review
hgc profile list
hgc source sync --profile review
hgc profile apply review --dir ./project --dry-run
hgc profile apply review --dir ./project
```

`profile add` creates a new profile; without `--add-skill/-AS`, it is empty. `profile update` edits an existing one. `-AS` accepts a source, unique discovered skill name, or `SOURCE:selector`. It merges the selected entry by default; `--replace` rewrites that entry's selection. `--include/-i` and `--exclude/-e` are repeatable and require `-AS`; `--replace` also requires it. Exact references supply an include selector automatically. Ambiguous skill names require an explicit source reference. Missing source content must be obtained with `source sync` before selector validation.

`profile update -RS/--remove-skill` removes a source entry or exact included selector and cannot accompany `-AS`. A description-only update is allowed. `profile show` prints saved TOML; `profile list` prints names, descriptions, and selected source names. To list individual skill candidates use `skill list --profile NAME`.

`profile remove NAME` deletes the profile directory and its contents; installed skills and source checkouts remain in place. Add, update, and remove support `--dry-run`. Editing configuration does not apply it to existing installation targets.

`profile apply` requires exactly one destination: `--path` for the exact skills container or `--dir` for a project's `.agents/skills`. It requires locally available source content and never syncs implicitly:

```sh
hgc profile apply review --path ./custom/skills --root ./kit
hgc profile apply review --dir ./project -cp
hgc profile apply review --dir ./project --link-mode junction
```

The default materialization mode is symlink, or junction on Windows. `-cp` and `--link-mode copy` create independent copies; `-cp` conflicts with explicit symlink or junction modes. Junction mode is for Windows; explicit Windows symlinks may require an elevated terminal. Applying a profile refuses to overwrite independent directories, can retarget existing links of the selected type, and leaves unselected installations in place. There is no tracking or pruning; installation is not transactional.

If multiple skill directories share an installation name, a TTY prompts for the source path for this invocation without changing the profile. Non-TTY application fails with guidance to narrow its include selectors. `--dry-run` prints the plan without writes and lists all conflicting candidates without prompting, even in a TTY.

## SFTP file sync

Initialize the reference VSCode-SFTP template in an existing project directory, then edit its placeholder connection values. Initialization does not connect to a server or include a password or private key. It refuses to replace an existing config unless `--force` is supplied; `--dry-run` prints the planned path and template without creating files:

```sh
hgc file init --root /path/to/project
hgc file init --root /path/to/project --dry-run
hgc file init --root /path/to/project --force
```

Run file sync from a project directory containing `.vscode/sftp.json`. The command uses the same `context` to `remotePath` mapping as VSCode-SFTP:

```sh
hgc file push --dry-run
hgc file push
hgc file push --git-changed --dry-run
hgc file pull
hgc file sync
```

`push` uploads, `pull` downloads, and `sync` synchronizes in both directions.

For one-off directory-tree sync, pass an SCP-style endpoint. This mode uses the current directory as the local root unless `--root` is given, and it completely bypasses `.vscode/sftp.json` even when that file is present or malformed:

```sh
hgc file push dev@server:/srv/project --dry-run
hgc file pull server:~/Projects/ws --root ./restore
hgc file sync server:C:/Projects/ws --exclude '*.tmp'
hgc file push '[2001:db8::1]:/srv/project' -P 2222 -i ~/.ssh/id_ed25519
```

The endpoint must contain a non-empty remote path. Use `host:.` for the remote home directory; `host:~/path`, SSH aliases, explicit `user@host`, bracketed IPv6, and Windows drive paths are supported. All temporary directions accept `-P/--port`, `-i/--identity`, repeatable `--exclude`, `--skip-create`, and `--ignore-existing`. The two one-way directions also accept `--delete` and `--update`, while `push` additionally accepts `--git-changed`; `sync` intentionally does not expose `--delete` or `--update`. These temporary-only options require an endpoint, and endpoint mode is mutually exclusive with `--profile`.

Temporary sync has safe non-deleting defaults and always excludes `.git` files/directories and every `.vscode/sftp.json`, including nested projects; later user negation rules cannot re-include them. It has no password option. Authentication uses the SSH agent, default keys, SSH config, or `--identity`; load encrypted private keys into the agent first. The endpoint user, `-P`, and `-i` take precedence over SSH config, while `HostName`, `User`, `Port`, `IdentityFile`, and `ProxyCommand` are read from the default `~/.ssh/config`. Port 22 and the current system user are the final fallbacks. Existing host-key verification still applies.

`push` and `pull` treat the named side as the source and make it win shared-path conflicts. By default they copy missing files and use whole-second modification time plus size to identify possible overwrites, while destination-only paths remain untouched. Before overwriting a shared regular file, the command first skips content comparison if the sizes prove that CRLF normalization cannot make the files equal. Otherwise it reads both copies and skips the action when their bytes match or their text differs only by CRLF versus LF line endings. Files containing a NUL byte are compared as binary data, a lone CR remains significant, and transferred bytes are never rewritten. This extra content read also applies to dry runs. `syncOption.update`, `ignoreExisting`, `skipCreate`, and `delete` retain their VSCode-SFTP meanings; `delete = true` removes destination-only paths, so preview that plan first. `sync` copies unique files to the other side and makes the precisely newer version of a shared file win, with local winning an exact timestamp tie; as in VSCode-SFTP, only `skipCreate` and `ignoreExisting` affect this mode.

**Detection limit:** same-size edits within the same modification-time second can be missed, including with `--git-changed`; that flag selects paths and does not force content comparison. There is currently no checksum/force option. Completion counts describe planned actions, not the number of files changed; deleting an already-absent path or a nonempty protected directory may leave the filesystem unchanged.

Use `hgc file push --git-changed` to restrict an upload to staged, unstaged, untracked, deleted, and renamed paths reported for the configured local `context`, or the temporary mode's local root. Git-ignored paths are excluded. A rename uploads the new path; removing the old remote path, like any deletion, still requires `syncOption.delete = true` in config mode or `--delete` in temporary mode. The Git path set only narrows the normal sync plan, so ignore and sync options remain authoritative. Scanning keeps changed paths and their parents and skips unrelated subdirectories before planning; each visited directory is still enumerated. If no Git changes exist, the command returns without connecting to SFTP. Git is never inspected during ordinary sync, so Git need not be installed and the project need not be a repository; when `--git-changed` is explicitly requested, missing Git, a non-repository context, or a Git inspection failure is reported before any remote connection. Each Git inspection command has a 120-second timeout.

Online sync uses one SFTP connection and applies transfers sequentially. The implementation supports SFTP configs, `context`, Windows-style remote paths, `ignore`, `ignoreFile`, `remoteTimeOffsetInHours`, `filePerm`, `dirPerm`, `useTempFile`/`openSsh`, password, private-key, SSH-agent, and `~/.ssh/config` authentication. Configured permissions are applied when remote files or directories are created or uploaded; permissions on existing remote directories are not reconciled. All online sync modes protect `.git` files/directories and `.vscode/sftp.json` at every depth on both sides, including case variants; user negations cannot re-include them. Worktree and submodule Git metadata files are not synchronized. SSH host keys must already be trusted in the user's known-hosts files. A single config is selected automatically. For arrays or nested `profiles`, use `--profile NAME`; ambiguous short names are rejected, `--profile CONFIG:PROFILE` selects a nested profile, and `--profile CONFIG:` selects the base config without its `defaultProfile`. Otherwise, `defaultProfile` is selected automatically. Use `--root <directory>` to target a config outside the current directory. Except for a clean `--git-changed` upload, `--dry-run` still connects and scans the remote side, but performs no file changes.

### Remote WSL over SFTP

A Windows OpenSSH alias whose `RemoteCommand` starts WSL is still a Windows SFTP endpoint: OpenSSH configures SFTP as a separate subsystem, and `hgc` does not interpret `RemoteCommand` or `RequestTTY`. To sync a WSL filesystem remotely, run a standard sshd inside the target WSL distribution and expose it through a reachable address or Windows port forwarding/firewall rule, then give that endpoint its own alias ([Microsoft OpenSSH Server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)):

```sshconfig
Host win-wsl
  HostName windows-host
  User <wsl-linux-user>
  Port 2222
```

```sh
hgc file push win-wsl:/home/<wsl-linux-user>/Projects/ws --dry-run
hgc file pull win-wsl:~/Projects/ws -r ./restore
```

Do not add `RemoteCommand` or `RequestTTY` to this alias. `hgc` does not install WSL sshd, configure Windows networking, or edit SSH config. On native Windows, a path such as `\\wsl.localhost\DISTRO\home\...` can be used as the local `--root`, but it is not the remote WSL transport described above, and cross-filesystem access may have a performance cost ([Microsoft WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems)).

## Offline sync bundles

Use `pack` and `apply` when SSH, SFTP, or network access is unavailable. `pack` reads local files only and creates a portable ZIP; transfer that ZIP through any external channel, then verify and apply it locally at the destination:

```sh
hgc file pack --root /path/to/source
hgc file pack -r /path/to/source -o release.zip --force
hgc file pack -r /path/to/source --git-changed --exclude '*.tmp'
hgc file apply release.zip --root /path/to/destination --dry-run
hgc file apply release.zip -r /path/to/destination --delete
```

The default output is `hgc-sync.zip` in the invocation directory. Existing output is refused unless `--force` is explicit; output uses a same-directory temporary file and atomic replacement. `--dry-run` reads and hashes the selected source files but creates no archive. The source defaults to the current directory. When `.vscode/sftp.json` exists, `pack` reuses only profile selection, `context`, `ignore`, and `ignoreFile`; it neither validates nor records connection or authentication fields and never opens an SFTP connection. A missing config means ordinary-directory mode. Invalid JSON or ambiguous profile selection is an error; `--no-config` bypasses config discovery completely and is mutually exclusive with `--profile`.

A normal pack is a full source snapshot. `--git-changed` instead creates a Git patch containing staged, unstaged, untracked, deleted, and renamed paths; old rename paths become deletion markers. Packing and applying a Git patch skip unrelated subdirectories during scanning. A clean or fully filtered worktree creates no bundle. Git is not needed for full packs or ordinary directories. Git metadata (`.git` files/directories), all nested `.vscode/sftp.json` configs, the output bundle itself, config ignore rules, and repeatable `--exclude` patterns remain excluded. Symlinks are never followed or packed: they are listed in the manifest and printed as warnings while pack still succeeds.

The ZIP contains a versioned `manifest.json` and file bytes under `payload/`. The uncompressed manifest is limited to 16 MiB; oversized manifests are rejected before decompression or destination writes. Packing, including dry runs, enforces the same limit. This limits the manifest only; total uncompressed payload size is not capped. The manifest records portable relative paths, type, size, nanosecond mtime, POSIX mode, SHA-256, effective ignore rules, deletion markers, and skipped symlinks, but no host, credentials, or absolute source path. SHA-256 detects corruption; the bundle is not signed, authenticated, or encrypted.

Before reading payloads, bundle verification rejects entries and deletion markers that target Git metadata, SFTP configs at any depth, or paths excluded by the manifest. Negation rules cannot override metadata protection, including case variants. Before any destination write, `apply` validates the complete archive, manifest version, entry set, safe paths, duplicate and cross-platform path collisions, sizes, and hashes. The destination directory may be absent and is created only during a real apply. The bundle wins shared-path conflicts by default. `--skip-create`, `--ignore-existing`, and `--update` retain the one-way sync meanings. For a full bundle, `--delete` removes destination-only, non-ignored paths; for a Git patch it applies only explicit deletion markers and never expands into a destination mirror. Writes are atomic and deletions run last. Text differing only by CRLF versus LF is left untouched, NUL-containing files use raw-byte comparison, and transferred bytes are never rewritten. New POSIX files restore source mode, overwritten files preserve destination mode, Windows does not force POSIX permissions, and mtime is restored where supported.

## Project artifact purge

`hgc file purge` finds rebuildable project artifacts such as dependency directories, build output, test and tool caches, and directories carrying a valid `CACHEDIR.TAG`. Start with a preview:

```sh
hgc file purge --dry-run
hgc file purge
hgc file purge ~/Work/client-a ~/scratch/project-b
hgc file purge --paths
```

Positional `PATH...` values are temporary scan roots for that invocation and replace both the saved path list and automatic discovery. Without positional paths, a nonempty per-user path file replaces automatic discovery; a missing or effectively empty file restores the automatic roots. Use `--paths` by itself to create or edit this file, show which configured roots exist, and reopen the effective list after the editor exits. It uses `$VISUAL`, then `$EDITOR`, then `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere. Blank lines and `#` comments are ignored, and each entry must be an absolute or `~` path.

The path file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows. Windows falls back to `~/.config/hagency/space-purge-paths` when `APPDATA` is unavailable.

Automatic discovery checks existing `~/www`, `~/dev`, `~/Projects`, `~/GitHub`, `~/Code`, `~/Workspace`, `~/Repos`, `~/Development`, `~/.codex/worktrees`, and `~/.claude/worktrees`, plus direct children of the home directory that contain project markers within two levels. It does not automatically search system or cloud-storage roots. A worktree location alone does not qualify a directory for deletion: candidates must match a supported artifact name or a valid `CACHEDIR.TAG`, have enclosing project evidence, and pass Git and filesystem checks. Being a Git worktree is not a blanket exclusion; Git protection excludes candidates containing tracked files. Inspect the exact candidate paths in the preview.

In a TTY, Questionary presents a multi-select list. Candidates whose last artifact activity is strictly more than seven days old are selected by default; recent or uncertain candidates remain unselected. A normal purge then prints every selected absolute path and asks for a second confirmation that defaults to No. With `--dry-run`, the multi-select still opens, but the selected entries are only previewed and the destructive confirmation is skipped. A non-TTY run skips the TUI and previews all candidates with their default selection status, even when `--dry-run` is omitted.

Purge deletes selected artifacts permanently rather than moving them to Trash or the Recycle Bin. It excludes Git-tracked candidates, including an entire candidate that contains a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, zero-size candidates, the global Xcode `DerivedData` directory, non-Composer `vendor` directories, and `bin` directories outside `.NET` projects. Generic artifact names are accepted only with project evidence. If permanent deletion fails partway through because of permissions, a filesystem change, or another I/O error, some contents may already be gone; the command reports the failure, continues with later selections, and exits with status 1.

## Local model proxy

`hgc service model-proxy start` starts a background process that exposes both OpenAI Responses and Chat Completions interfaces for every configured provider. Provider selection comes from the URL, never from `model`; the model value is forwarded unchanged.

Create `hagency-model-proxy.toml` beside `hagency-config.toml`:

```toml
version = 1
default_provider = "openai"

[providers.openai]
adapter = "openai"
api_key = { env = "OPENAI_API_KEY" }

[providers.corp]
adapter = "openai_compatible"
base_url = "https://llm.corp.example/openai/v1"
hook = "corp.py"

[providers.corp.headers]
"X-Tenant" = { env = "CORP_TENANT" }
```

Environment-backed values are resolved from `.env` beside `hagency-model-proxy.toml` and then the process environment, with the process environment taking precedence. The same merged, read-only mapping is available to trusted Hooks as `init.env`, so provider-specific authentication can consume workspace credentials without loading files itself. Keep `.env` out of version control.

Then point an OpenAI-compatible client at one of these base URLs:

```text
http://127.0.0.1:8765/v1
http://127.0.0.1:8765/openai/v1
http://127.0.0.1:8765/corp/v1
```

Manage the background process with the same workspace root or explicit config path:

```sh
hgc service model-proxy start -r <workspace>
hgc service model-proxy stop -r <workspace>
hgc service model-proxy restart -r <workspace>
```

Linux uses a detached session; Windows uses a detached, no-console process group. State and logs are stored below `XDG_STATE_HOME`/`~/.local/state` on Linux and `LOCALAPPDATA` on Windows. Set `HAGENCY_STATE_HOME` to an absolute path to override that root. `start` reports the exact log path; logs rotate at 10 MiB with three backups.

The bare `/v1` routes use `default_provider`; `/<provider>/v1` selects one explicitly. `POST /responses`, `POST /chat/completions`, and `GET /models` are available without per-client configuration. A matching upstream protocol uses a raw entity path; the other interface is converted. `/models` proxies the adapter's model-list operation and uses the same request, authentication, and response Hook stages. A Hook may instead implement `fetch_models(ctx)` and return model ID strings when the provider has no standard model-list endpoint; because that compact contract contains no creation metadata, synthesized model records use `created = 0` as a stable unknown value. Additional resource operations under the native protocol family are passed through without cross-protocol emulation.

Downstream credential headers are stripped by default. Use `forward_credential_headers` for explicit per-provider forwarding, static/env headers for normal authentication, or a trusted Python hook under `<config-dir>/hooks/` for custom request, signing, and response handling. Hooks receive the merged environment as the read-only `init.env` mapping, run in-process, and take effect after a restart. Defining `process_response` buffers non-SSE responses before invoking the hook and enforces a 64 MiB response limit; omit that method when response inspection is unnecessary. The server only accepts loopback listen addresses.

`adapter = "openai"` supplies the Responses protocol and OpenAI API root; `adapter = "openai_compatible"` defaults to Chat Completions and requires `base_url`. Override `protocol` at provider level when needed. To add a provider family, add one module under [`model_proxy/providers`](src/hagency_cli/model_proxy/providers/README.md) that exports `ADAPTER`; the filename becomes the adapter value and no central registry change is needed.

An explicit `--config /path/to/hagency-model-proxy.toml` needs no Hagency workspace and is mutually exclusive with `--root`. Relative config paths use the invocation directory. Use the same resolved config path and state directory for lifecycle commands. `start` and `restart` return after the worker binds its port; starting an already running service fails. Stopping an already stopped worker succeeds. Restart defaults to `127.0.0.1:8765` on each invocation, so repeat custom `--host` and `--port` settings. A failed restart can leave the old worker stopped; read the reported log for diagnosis.

### Provider hooks

A hook file named by `hook = "corp.py"` lives under `<config-dir>/hooks/`. It exports a `Hook` class that accepts `HookInit`. For a provider using a custom credential header, a minimal `hooks/corp.py` can be:

```python
from hagency_cli.model_proxy import AuthPatch, HeaderPatch


class Hook:
    def __init__(self, init):
        self.token = init.env["CORP_TOKEN"]

    async def authenticate(self, ctx, request):
        return AuthPatch(
            headers=HeaderPatch(set=(("X-Corp-Token", self.token),))
        )
```

`prepare_request` and `process_response` see provider-native data. `authenticate` sees the final request body and may return only header/query patches. A hook can implement `fetch_models(ctx)` returning `list[str]` to supply model IDs; missing methods preserve the relevant raw path. Matching-protocol request/response entity bytes remain intact except where explicitly configured body or SSE hooks modify them. Cross-protocol requests use the built-in bridge.

Hooks are trusted in-process code. Loading and contract validation happen before listening; failures are fail-closed, and files are not hot-reloaded. Keep deployment secrets in environment-backed configuration and keep model routing out of provider adapters. The public hook types remain in `hagency_cli.model_proxy`; provider adapter details are in the [provider guide](src/hagency_cli/model_proxy/providers/README.md).

## Troubleshooting

| Symptom | Next step |
| --- | --- |
| `not a hagency workspace` | Run `hgc init` for a new workspace, or pass `--root` for an existing one. Wheel installs have no editable-checkout fallback. |
| Skills missing from discovery | Check `source list` paths and run `source sync` for the relevant sources; discovery never fetches content. |
| Ambiguous skill/source selection | Use `SOURCE:selector`, `--source-name`, or narrower `--skill`/profile include selectors. Non-TTY installation needs explicit selection when multiple skills exist. |
| Destination differs from expectation | `--path` is the final container; `--dir` appends `.agents/skills`. Both resolve from the invocation directory independently of `--root`. |
| Existing destination refused | Inspect the destination; independent directories are protected. Choose another destination for a separate copy. |
| Sync cannot fast-forward | Inspect local commits. Use `--reanchor` only for a disposable, clean checkout whose local-only commits can be discarded. |
| SFTP profile selection is ambiguous | Use `--profile CONFIG:PROFILE` or `CONFIG:` for the base config. Temporary endpoints cannot accompany `--profile`. |
| Online sync misses a same-size edit | The mtime/size filter can miss edits within one second. `--git-changed` narrows paths but does not force comparison. |
| SFTP host key or authentication fails | Establish trust in known-hosts and check SSH config/agent credentials. Preload encrypted keys; a Windows `RemoteCommand wsl` alias does not provide WSL SFTP. |
| Purge previews without deleting | Non-TTY runs always preview. A TTY run requires selection and a separate confirmation for permanent deletion. |
| Proxy fails to start or reload | Check the printed log, config, environment, hooks, and port. Use the same config path and repeat custom listener options when restarting. |

### Dry runs and failures

Dry-run semantics depend on the operation:

| Command family | Preview behavior |
| --- | --- |
| Workspace/source/profile config edits | Print proposed changes without writing; source previews do not fetch. |
| `skill add` / `profile apply` | Preview without target writes or network access; conflicts never prompt. Missing checkouts can make a skill-install preview provisional. |
| Online `file push/pull/sync` | Connect and read both sides, including content comparisons, without writes/deletes. A clean Git-filtered upload skips connection. |
| `file pack/apply` | Read, hash, or verify local data without creating the ZIP or changing the destination. |
| `file purge` | TTY selection still opens, followed by preview; non-TTY runs preview with or without the flag. |

The model-proxy lifecycle and `file purge --paths` have no dry-run mode. Invalid CLI syntax or option combinations generally exit with status 2; operation failures generally use status 1. A failed batch may have completed earlier work: source sync continues other sources, skill acquisition retains registration/checkouts, and purge or file transfers can leave partial results. Inspect the progress report before retrying.

## Development

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

Questionary imports happen when interactive adapters are used. SFTP imports Paramiko when connecting. The CLI can load lightweight proxy daemon/configuration code; ordinary commands and help do not import the HTTP server. The worker remains `hagency_cli.model_proxy.worker`, with unchanged hook contracts and service state locations. Python consumers import the owning modules directly. Importing `hagency_cli.model_proxy.server` loads the HTTP implementation. Hook-facing types such as `AuthPatch` and `HeaderPatch` are exported from `hagency_cli.model_proxy`.

### Maintaining help

Command docstrings hold the full operational help; `short_help` keeps command lists concise. Aliases reuse those docstrings. The command group's formatter combines the registered aliases into one row while preserving command parsing and completion. A registration check verifies that its display map matches shared callbacks and Typer instances, including within each command group. Options use Typer's native multiple name declarations. Example blocks use Click's `\b` paragraph marker to preserve newlines. Help is part of the package and never loads Markdown files at runtime.

When changing a command, update its help and the matching user-guide section together. Keep runtime behavior in the owning operation module, and keep help usable from an empty directory without loading interactive, SFTP, or HTTP implementations.

### Filesystem and transport implementation

Explicit SFTP agents belong to individual connections; connecting does not modify `SSH_AUTH_SOCK`. Unix agent sockets use the connection timeout. Native Windows retains Paramiko's Pageant/OpenSSH discovery. The adapter's small integration with Paramiko's existing agent authentication is tested with Paramiko 3.5.1 and 4.0.0, including signing through a temporary local OpenSSH agent. Online sync preserves symlink targets, including absolute and dangling links, without scanning through them. Offline bundles continue to skip symlinks.

Purge's descriptor-based removal claims a unique temporary name for the emptied artifact, checks its identity while retaining the open descriptor, and removes that name. This preserves directories concurrently recreated at the original path. If the final identity check or removal fails, the report identifies the quarantine path where remaining contents are retained.

### Validation

From this directory:

```sh
uv run --frozen python -m unittest discover -s tests
uv build --out-dir /tmp/hagency-cli-dist
git diff --check
```

The suite includes command and alias help, CLI `main()` parsing and command removal, dependency and lazy-import boundaries, workspace discovery, local Git acquisition/update behavior, selection/cancellation, dry runs, installation modes, SFTP credential/Git protection, bundle validation before writes, purge revalidation, and proxy lifecycle coverage. Mocks target the module that owns each dependency.

Release validation also installs the wheel in an isolated environment outside a Hagency workspace and tests an isolated editable installation. Native filesystem behavior is established only for the host platform; mocked Windows/macOS branches do not establish native execution, and local/fake transports do not establish live SFTP or upstream provider behavior.
