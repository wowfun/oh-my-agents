# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Hagency CLI

The `hgc` CLI manages Hagency workspaces, sources, skill discovery and installation, profiles, online and offline project file sync, generated profile skill outputs, and project artifact cleanup. Source registry entries live in [`hagency-config.toml`](hagency-config.toml), and profile configs live under `profiles/<name>/config.toml`.

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace>
hgc skill add <source>:<selector> --global
hgc p apply -p <xxx>/skills <profile>
hgc p apply -d <workspace> <profile>
hgc file sync --dry-run
hgc file purge --dry-run
hgc service model-proxy start
```

The editable installation lets `hgc` find this Hagency Kit checkout when it is
run outside a workspace, so `-r` is normally unnecessary. Workspace precedence
is an explicit `--root`, then the current directory and its parents, then the
checkout that provides the installed `hagency_cli` source. The fallback only
checks that checkout's root config; it never searches ancestors of an installed
package. A non-editable installation requires `--root` outside a workspace.

Install completion for the current shell, or print a shell-specific completion script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It uses the same workspace precedence as commands and respects `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted.

### Commands and migration

```text
hgc init
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|purge
hgc service model-proxy start|stop|restart
```

`source sync` updates persistent Git checkouts. `file sync` synchronizes project files over SFTP. Keep using `s`/`p`, `ls`, `rm`, and profile `u` aliases.

| Removed command | Replacement |
| --- | --- |
| `hgc sync local-to-remote` / `l2r` | `hgc file push` |
| `hgc sync remote-to-local` / `r2l` | `hgc file pull` |
| `hgc sync both` | `hgc file sync` |
| `hgc sync init\|pack\|apply` | `hgc file init\|pack\|apply` |
| `hgc space purge` | `hgc file purge` |
| `hgc profile init` / `hgc p init` | `hgc profile apply` / `hgc p apply` |
| `hgc serve ACTION --model-proxy` | `hgc service model-proxy ACTION` |

The old entrypoints are removed. Operation options retain their meanings; repeat `--exclude` for file patterns and `--skill` for installation selectors. Legacy multi-value `-i`/`-e` expansion applies only to `profile`/`p add|update|u`. Config formats, bundle v1, proxy state locations, and the `space-purge-paths` filename stay unchanged. Module ownership and validation commands are documented in [the CLI architecture guide](tools/hagency-cli/README.md).

### Quick skill installation

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

### SFTP file sync

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

### Offline sync bundles

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

### Remote WSL over SFTP

A Windows OpenSSH alias whose `RemoteCommand` starts WSL is still a Windows SFTP endpoint: OpenSSH configures SFTP as a separate subsystem, and `hgc` does not interpret `RemoteCommand` or `RequestTTY`. To sync a WSL filesystem remotely, run a standard sshd inside the target WSL distribution and expose it through a reachable address or Windows port forwarding/firewall rule, then give that endpoint its own alias ([Microsoft OpenSSH Server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)):

```sshconfig
Host win-wsl
  HostName desktop-7divr3r
  User <wsl-linux-user>
  Port 2222
```

```sh
hgc file push win-wsl:/home/<wsl-linux-user>/Projects/ws --dry-run
hgc file pull win-wsl:~/Projects/ws -r ./restore
```

Do not add `RemoteCommand` or `RequestTTY` to this alias. `hgc` does not install WSL sshd, configure Windows networking, or edit SSH config. On native Windows, a path such as `\\wsl.localhost\DISTRO\home\...` can be used as the local `--root`, but it is not the remote WSL transport described above, and cross-filesystem access may have a performance cost ([Microsoft WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems)).

### Project artifact purge

`hgc file purge` finds rebuildable project artifacts such as dependency directories, build output, test and tool caches, and directories carrying a valid `CACHEDIR.TAG`. Start with a preview:

```sh
hgc file purge --dry-run
hgc file purge
hgc file purge ~/Work/client-a ~/scratch/project-b
hgc file purge --paths
```

Positional `PATH...` values are temporary scan roots for that invocation and replace both the saved path list and automatic discovery. Without positional paths, a nonempty per-user path file replaces automatic discovery; a missing or effectively empty file restores the automatic roots. Use `--paths` by itself to create or edit this file, show which configured roots exist, and reopen the effective list after the editor exits. It uses `$VISUAL`, then `$EDITOR`, then `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere. Blank lines and `#` comments are ignored, and each entry must be an absolute or `~` path.

The path file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows. Windows falls back to `~/.config/hagency/space-purge-paths` when `APPDATA` is unavailable.

Automatic discovery checks existing `~/www`, `~/dev`, `~/Projects`, `~/GitHub`, `~/Code`, `~/Workspace`, `~/Repos`, `~/Development`, `~/.codex/worktrees`, and `~/.claude/worktrees`, plus direct children of the home directory that contain project markers within two levels. It does not automatically search system or cloud-storage roots.

In a TTY, Questionary presents a multi-select list. Candidates whose last artifact activity is strictly more than seven days old are selected by default; recent or uncertain candidates remain unselected. A normal purge then prints every selected absolute path and asks for a second confirmation that defaults to No. With `--dry-run`, the multi-select still opens, but the selected entries are only previewed and the destructive confirmation is skipped. A non-TTY run skips the TUI and previews all candidates with their default selection status, even when `--dry-run` is omitted.

Purge deletes selected artifacts permanently rather than moving them to Trash or the Recycle Bin. It excludes Git-tracked candidates, including an entire candidate that contains a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, zero-size candidates, the global Xcode `DerivedData` directory, non-Composer `vendor` directories, and `bin` directories outside `.NET` projects. Generic artifact names are accepted only with project evidence. If permanent deletion fails partway through because of permissions, a filesystem change, or another I/O error, some contents may already be gone; the command reports the failure, continues with later selections, and exits with status 1.

### Local model proxy

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

`adapter = "openai"` supplies the Responses protocol and OpenAI API root; `adapter = "openai_compatible"` defaults to Chat Completions and requires `base_url`. Override `protocol` at provider level when needed. To add a provider family, add one module under [`model_proxy/providers`](tools/hagency-cli/src/hagency_cli/model_proxy/providers/README.md) that exports `ADAPTER`; the filename becomes the adapter value and no central registry change is needed.

`[defaults].depth` sets the default sync depth; transient Git network failures are retried automatically. Use `hgc source sync -s <slice>` to resume a selected source range after a failure. When a Git URL's inferred repo name already exists, `source add` falls back to `owner/repo`; pass `--name` to choose a custom source name.

Use an optional Windows checkout override when the same config is shared across platforms:

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Checkout directory precedence is `--checkout-dir`, then `checkout_dir_windows` on native Windows, then `checkout_dir`. WSL is not treated as native Windows and continues to use `checkout_dir`. On native Windows, Git Bash paths such as `/d/Projects/references` are normalized to `D:/Projects/references`. If `checkout_dir_windows` is omitted, native Windows falls back to `checkout_dir`. This is a config-only override; there is no new CLI flag.

Normal sync refuses non-fast-forward updates. If an upstream source rewrites history and its checkout is disposable, rerun the failed selection with `--reanchor`. Reanchoring requires a checkout with no staged, unstaged, or untracked changes, then replaces local-only commits with the fetched upstream history. The option works with source names, `--profile`, and `--slice`; `--dry-run` only describes the conditional behavior.

`skill add` installs selected skills from the inputs described above. It links into the invocation directory's `.agents/skills` by default. Use `-p/--path` for an exact skills container, to which only the skill name is appended; use `-d/--dir` for a workspace root whose destination is `<workspace>/.agents/skills`; or use `--global` for `~/.agents/skills`. These three options are mutually exclusive. Relative destination paths resolve against the invocation directory, and `~` expands to the current user's home. `--root` and `--checkout-dir` resolve sources and never change the installation destination. Shortcut installation persists the resolved checkout override when registering a new remote source. Non-Windows platforms use symlinks; Windows uses junctions.

`profile apply` requires exactly one destination: `-p/--path` is the final skills container and `-d/--dir` is a workspace root that expands to `<workspace>/.agents/skills`. Destination paths follow the same invocation-directory and `~` expansion rules, while `--root` and `--checkout-dir` remain discovery-only. Use `--dir` when the supplied path is a workspace root. Applying a profile leaves unselected installations in place and refuses to overwrite independent copies stored as real directories. Existing symlinks/junctions pointing to a different source may be retargeted. There is no installation tracking or pruning.

If multiple discovered skill directories have the same install name, an interactive terminal prompts you to choose the source path for that application. The profile is not rewritten. A non-interactive installation fails with guidance to rerun in a terminal or narrow the profile's `include` selector; with `--dry-run`, it lists every conflicting source candidate without prompting, including in a TTY.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | Using the Hagency Kit CLI for sources, profiles, skills, online or offline project file sync, project artifact cleanup, profile application, or the local model proxy | Helps agents manage Hagency workspace content, sync project files, safely preview project artifact cleanup, and run provider-level Responses/Chat proxy endpoints. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

A profile lists the source names and skill selectors it enables in `profiles/<name>/config.toml`. After applying a profile, selected skills are materialized in the requested skills container; `-d/--dir` uses the workspace's `.agents/skills` container.
