---
name: hagency-cli
description: Use the Hagency Kit CLI for workspace, source, skill, profile, online or offline project file sync, project artifact purge, and local model-proxy workflows. Trigger for `hgc`, source syncs, `.vscode/sftp.json` file sync, offline sync bundles, skill discovery or installation, profile skill edits, profile application, project artifact cleanup, `hagency-config.toml`, `hagency-model-proxy.toml`, provider-level OpenAI Responses or Chat Completions proxying, generated profile skill outputs, and updates to `skills/hagency-cli/SKILL.md`.
---

# Hagency CLI

Use the repo-local `hgc` CLI to inspect and manage Hagency workspaces, sources, skills, profiles, online or offline project file sync, generated profile skill links, rebuildable project artifacts, and the loopback model proxy. If the CLI cannot satisfy the user's request, explain the gap and ask whether to improve `hagency-cli`.

## Workspace Context

Resolve the workspace from the current directory when it is inside a tree with `hagency-config.toml`. Use `-r` when the workspace root is elsewhere. Source registry entries live in `hagency-config.toml`.

Generated profile output belongs in the selected skills container. `-p/--path` selects that container directly; `-d/--dir` selects a workspace and uses its `.agents/skills` container. Treat generated entries as links or copies, not source skill files.

For source checkout discovery, the existing `--checkout-dir` option has highest precedence. Without it, native Windows uses the optional `[defaults].checkout_dir_windows`; other platforms, including WSL, use `[defaults].checkout_dir`. Native Windows also falls back to `checkout_dir` when the override is absent. This does not add a CLI flag.

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Native Windows normalizes Git Bash paths such as `/d/Projects/references` to `D:/Projects/references`. Keep the base `checkout_dir` usable by Linux, macOS, and WSL rather than putting a Windows-only path there.

## Command Layout

```text
hgc init
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|purge
hgc service model-proxy start|stop|restart
```

Use the new paths directly: old top-level `sync`, `space`, `serve`, direction aliases, `profile init`, and `--model-proxy` have been removed. `source sync` still updates Git repositories. File patterns use repeatable `--exclude`; legacy multi-value `-i`/`-e` expansion is limited to `profile`/`p add|update|u`. Configuration formats, bundle v1, daemon state paths, and `space-purge-paths` are unchanged.

## Inspect Sources, Skills, Profiles

Use `s` and `p` for the top-level source and profile aliases. Use `ls` for list commands. Use `hgc skill ls` to scan `SKILL.md` directories before editing profile selectors.

```sh
hgc s ls -r <root>
hgc s show <source> -r <root>
hgc skill ls -s workspace -r <root>
hgc skill ls -s <source> -r <root>
hgc skill ls -p <profile> -r <root>
hgc skill ls --checkout-dir <checkout-dir> -r <root>
hgc p ls -r <root>
hgc p show <profile> -r <root>
```

## Sync Sources

Sync remote sources before relying on profile application or skill-name inference. For profile-scoped sync, keep the long `--profile` option because `source sync -s` is already the slice selector. Use `--depth` for shallow checkouts and `-s` with 1-based indexes to resume a failed subset.

```sh
hgc s sync --profile <profile> --depth 1 -r <root>
hgc s sync <source> --depth 1 -r <root>
hgc s sync --profile <profile> -s 4: -r <root>
hgc s sync --profile <profile> -s 1,3: -r <root>
```

Normal sync refuses non-fast-forward updates. When the error lists a `--reanchor` retry command, use it only if every selected checkout is disposable and may lose local-only commits.

```sh
hgc s sync <source> --reanchor -r <root>
hgc s sync --profile <profile> -s 4: --reanchor -r <root>
```

Reanchoring requires no staged, unstaged, or untracked changes. It does not save sync state or create recovery refs. `--dry-run --reanchor` only describes what an actual sync may do after fetching.

## Sync Project Files over SFTP

If an existing project has no SFTP config, initialize the reference placeholder template with `hgc file init --root <project>`, then edit it before syncing. Initialization requires an existing project directory, never connects to the server, refuses an existing config unless `--force` is explicit, and supports a non-writing `--dry-run`.

Run sync from the directory that contains `.vscode/sftp.json`, or pass that directory with `--root`. Preview any mutating sync first. A sync dry run connects and reads the remote tree but performs no writes or deletes.

```sh
hgc file init --root <project>
hgc file push --dry-run
hgc file push
hgc file push --git-changed --dry-run
hgc file pull
hgc file sync
```

Use `file push` for upload, `file pull` for download, and `file sync` for bidirectional synchronization.

For temporary directory-tree sync, pass an SCP-style endpoint. This completely bypasses `.vscode/sftp.json`, including an invalid file, and uses the current directory or `--root` as the local root:

```sh
hgc file push [user@]host:/remote/path --dry-run
hgc file pull host:~/remote/path -r ./restore
hgc file sync host:C:/Windows/path --exclude '*.tmp'
```

Require a non-empty endpoint path; use `host:.` for the remote home directory. SSH aliases, `user@host`, bracketed IPv6, `~/path`, and Windows drive paths are valid. Every temporary direction supports `-P/--port`, `-i/--identity`, repeatable `--exclude`, `--skip-create`, and `--ignore-existing`. One-way commands additionally support `--delete` and `--update`; `push` also supports `--git-changed`. Do not use `--delete` or `--update` with `sync`. Temporary-only options without an endpoint are errors, as is combining an endpoint with `--profile`.

Temporary mode defaults to no deletion and always excludes `.git` files/directories and `.vscode/sftp.json` at every depth, including case variants; user negations cannot re-include them. There is no plaintext password option. Prefer an SSH agent, SSH config, default keys, or `--identity`, and pre-load encrypted keys into the agent. Endpoint user, `-P`, and `-i` override SSH config; otherwise use `HostName`, `User`, `Port`, `IdentityFile`, and `ProxyCommand` from `~/.ssh/config`, followed by port 22 and the current system user. Host keys must already be trusted.

One-way sync treats the named side as the source and honors `syncOption.delete`, `skipCreate`, `ignoreExisting`, and `update`; destination-only paths remain unless `delete` is true. Bidirectional sync makes the precisely newest shared file win, gives local the exact-timestamp tie, and uses only `skipCreate` and `ignoreExisting`. Before overwriting a shared regular file, all directions read both copies and skip byte-identical text or text that differs only by CRLF versus LF; NUL-containing files use raw binary comparison, lone CR remains significant, and transfer never rewrites line endings. This comparison also runs during dry-run.

`file push --git-changed` restricts the normal plan to staged, unstaged, untracked, deleted, and renamed paths in the configured local `context`, or in the temporary local root. Ignored paths stay excluded and old rename paths are deleted only when `syncOption.delete` is true in config mode or `--delete` is present in temporary mode. A clean Git worktree returns without connecting. Ordinary sync never invokes Git and remains usable without Git or outside a repository. If `--git-changed` is explicit, treat missing Git, a non-repository context, or Git inspection failure as a safe error before connecting; never silently widen it to a full upload.

The command also honors `context`, `remotePath`, `ignore`, `ignoreFile`, time offsets, configured permissions on newly created or uploaded remote paths, temp-file uploads, private keys, passwords, SSH agents, and SSH config. Every online mode protects `.git` files/directories and `.vscode/sftp.json` at every depth on both sides, including case variants; negation rules cannot re-include them. This also excludes worktree and submodule Git metadata files. Host keys must already be trusted. Use `--profile NAME` for config arrays or nested profiles. Ambiguous short names fail safely; use `--profile CONFIG:PROFILE` for a nested profile or `--profile CONFIG:` for the base config without its `defaultProfile`.

For a remote WSL filesystem, require a standard SSH/SFTP endpoint served by sshd inside WSL. A Windows OpenSSH alias using `RemoteCommand wsl ...` remains a Windows SFTP endpoint because this workflow ignores `RemoteCommand` and `RequestTTY`. Recommend a separate alias such as `win-wsl` pointing to the WSL sshd port, without those shell-only options. Do not install WSL sshd, configure port forwarding/firewall rules, or edit the user's SSH config automatically. A native-Windows `\\wsl.localhost\DISTRO\...` path may be used as a local `--root`, but it is not a remote WSL transport and may be slower across the filesystem boundary.

Same-size edits within the same modification-time second can be missed, including with `--git-changed`; that flag filters paths and does not force content comparison. There is no checksum/force option. Completion summaries count planned actions, not changed files.

## Create and Apply Offline Sync Bundles

Use an offline bundle when the two sides cannot use SSH/SFTP. `pack` reads local files only and never opens a remote connection. Transfer the ZIP through a user-chosen channel, then run `apply` locally on the destination. Bundle verification rejects entries and deletion markers targeting Git metadata, SFTP configs at any depth, or excluded paths before reading payloads. Negation rules cannot bypass this protection. Treat this as a one-way source-to-destination operation; reverse direction requires a new pack on the other side.

```sh
hgc file pack -r <source>
hgc file pack -r <source> -o <bundle.zip> --force
hgc file pack -r <source> --git-changed --exclude '<pattern>'
hgc file apply <bundle.zip> -r <destination> --dry-run
hgc file apply <bundle.zip> -r <destination> --delete
```

The default output is `hgc-sync.zip` in the invocation directory. Existing output requires `--force`; `--dry-run` hashes the planned files without writing a ZIP. When a project config exists, reuse only profile selection, `context`, `ignore`, and `ignoreFile`; do not validate, record, or use host and authentication settings. Missing config means ordinary-directory mode. Use `--no-config` to bypass even malformed config, but never combine it with `--profile`.

By default, pack a full snapshot. Use `--git-changed` for a Git patch containing staged, unstaged, untracked, deleted, and renamed paths; old rename paths become deletion markers. A clean or fully filtered Git selection creates no bundle. Always exclude `.git/`, `.vscode/sftp.json`, and an output bundle inside the source tree, regardless of negation rules. Apply config ignore rules and repeatable `--exclude` patterns with Gitignore semantics. Never follow or pack symlinks; report each skipped path as a warning and preserve the list in the manifest.

An offline ZIP has `manifest.json` at its root and file bytes under `payload/`. The uncompressed manifest is capped at 16 MiB and rejected before decompression when oversized; packing and dry runs enforce the same cap. Total uncompressed payload size is not capped. Metadata and exclusion checks run before payload reads. Before any destination mutation, validate the whole archive, its exact entry set, version, paths, portable path collisions, sizes, and SHA-256 hashes. Do not imply that checksums authenticate, sign, or encrypt a bundle. Apply uses atomic file replacement and performs deletions last. `--skip-create`, `--ignore-existing`, and `--update` keep the one-way sync meanings. For full bundles, `--delete` mirrors non-ignored destination paths; for Git patches, it applies only manifest deletion markers. CRLF and LF are equivalent for NUL-free text comparisons, binary files use raw bytes, and content is never rewritten. On POSIX, new files receive source mode while replacements preserve destination mode; restore mtime when supported.

## Install Skills

Use `skill add` with a unique discovered skill name, exact `SOURCE:selector`, registered source, Git URL, GitHub `owner/repo`, or explicit local directory. A Hagency workspace must already be resolvable; do not initialize one implicitly. The default destination is the invocation directory's `.agents/skills`. Use `-p/--path` for an exact skills container, `-d/--dir` for a workspace whose destination is `<workspace>/.agents/skills`, or `--global` for the current user's `~/.agents/skills`. These three options are mutually exclusive. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. `-r/--root` and `--checkout-dir` resolve sources, never the destination; a new remote source persists its resolved checkout override.

```sh
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace> -r <root>
hgc skill add <skill> --global -r <root>
hgc skill add <skill> --dry-run
hgc skill add owner/repo --skill one --skill nested/two
hgc skill add https://github.com/owner/repo.git --ref main --all
hgc skill add ./local-skills --all
```

Repeat `--skill/-s` for source-relative selectors or use `--all`; they are mutually exclusive and cannot be combined with an exact skill input. Selectors must remain source-relative; absolute paths, escaping `..` paths, and symlink escapes are rejected. Without filters, one discovered skill installs directly; multiple skills require TTY multiselect with nothing preselected, or explicit selectors/`--all` in non-TTY use. Empty selection or cancellation installs nothing. Resolve every selection and duplicate-name conflict before writing installation targets; `--all` does not bypass conflict handling. Installation uses symlinks except on Windows, where it uses junctions.

Registered source names and `SOURCE:selector` take precedence over GitHub shorthand. Explicit local paths (`./`, `../`, `~`, absolute and Windows paths) resolve from the invocation directory. Reuse the most specific containing source and discover only inside the input subtree; workspace is the fallback. Tied matches require `--source-name`. New local sources default to the directory name.

Remote inputs reuse the unique source matching URL and explicit ref. Only GitHub trailing `/` and `.git` are normalized within the same transport; generic URLs match exactly, and HTTPS/SSH are distinct. Multiple matches require `--source-name`. New remote names try repository name, then `owner/repo`, then require an explicit name. Reject `workspace`, path traversal, and ambiguous names. New remote sources use workspace ref/depth defaults; an occupied unregistered checkout fails before registration. A new remote source's `--checkout-dir` is persisted as the resolved checkout path; an existing source's override only affects this invocation.

Only obtain missing checkouts. Never retarget an existing source or update its content during installation; explicit ref/checkout mismatches require `source sync` first. After static validation, register, obtain, select, then install. On fetch failure or cancellation, retain the registration and any checkout, and report completed stages. Do not promise rollback, uninstall, or installed-skill tracking. `skill list/ls` remains a discovery view.

Skill `--dry-run` never accesses the network or writes configuration/targets. It may use read-only Git commands to verify an explicit ref in an existing checkout. Local subdirectories inside remote checkouts also accept `--ref`. If the checkout is missing, describe the pending registration and fetch and state that candidates and the installation plan remain unverified.

## Edit Profiles

Use `p add` for new profile configs and `p u` for profile updates. `-AS` adds or merges a source, skill name, or `SOURCE:selector`; `-RS` removes one. Use `-i` and `-e` for include and exclude selectors. Use `--replace` only when the existing entry should be rewritten.

```sh
hgc p add <profile> --description "Profile description." -AS <source> -r <root>
hgc p u <profile> -AS <source> -i <include-selector> -e <exclude-selector> -r <root>
hgc p u <profile> -AS <source>:<selector> --replace -r <root>
hgc p u <profile> -RS <source> -r <root>
hgc p rm <profile> -r <root>
```

Skill-name inputs can resolve to a source when the name is unique. If the CLI reports ambiguity, rerun with the `SOURCE:selector` form shown in the error.

## Apply Profile Skills

Use `p apply` to materialize profile-selected skills. The command requires exactly one destination option: `-p/--path` names the final skills container and appends only each skill name, while `-d/--dir` names a workspace root and writes to `<workspace>/.agents/skills`. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. These rules are independent of `-r/--root` and `--checkout-dir`, which only control profile and source discovery. Symlinks are the default except on Windows, where junctions are the default. Use `-cp` when the target should get independent copies that can evolve separately from the source.

```sh
hgc p apply -p <xxx>/skills <profile> -r <root>
hgc p apply -d <workspace> <profile> -r <root>
hgc p apply -p <xxx>/skills <profile> -r <root> -cp
hgc p apply -d <windows-workspace> <profile> -r <windows-root>
hgc p apply -d <git-bash-workspace> <profile> -r <git-bash-root>
```

The CLI does not append `.agents/skills` to a `--path` value; use `--dir` when the input is a workspace root. Applying a profile leaves unselected installations in place and refuses to overwrite independent copies stored as real directories. Existing symlinks/junctions pointing to a different source may be retargeted. There is no installation tracking or pruning.

When multiple discovered directories have the same skill name, an interactive terminal asks which source path to install for that invocation without changing the profile. Non-interactive installation fails instead of guessing; rerun it in a terminal or narrow the profile's `include` selector. `--dry-run` lists all conflicting source candidates and continues the preview without prompting, including in a TTY.

## Command Completion

Use `hgc --install-completion` to install completion for the current shell, or `hgc --show-completion <shell>` to inspect the generated script. Completion includes command aliases and local source, profile, skill, selector, and directory values. It is read-only and silently returns no dynamic candidates when workspace data is missing, invalid, unreadable, or unsynced.

## Purge Project Artifacts

Use `hgc file purge` to remove rebuildable dependency directories, build output, tool caches, and valid `CACHEDIR.TAG` directories from projects. Always preview first because selected artifacts are permanently deleted rather than moved to Trash or the Recycle Bin.

```sh
hgc file purge --dry-run
hgc file purge
hgc file purge <path>...
hgc file purge --paths
```

Positional paths are temporary scan roots and replace both the saved path list and automatic discovery for that invocation. With no positional paths, a nonempty per-user `space-purge-paths` file replaces automatic discovery; a missing or effectively empty file restores it. `--paths` is an exclusive configuration mode: it creates the commented path file when needed, reports current root status, opens `$VISUAL`, `$EDITOR`, `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere, then reloads the result. Entries must be absolute or `~` paths, one per line; blanks and `#` comments are ignored.

The config file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows, with `~/.config/hagency/space-purge-paths` as the Windows fallback. Automatic discovery uses the standard home project roots, `~/.codex/worktrees`, `~/.claude/worktrees`, and eligible direct home-directory project containers. It does not automatically enter system or cloud-storage roots.

In an interactive TTY, Questionary preselects only candidates whose last artifact activity is strictly more than seven days old. Recent and uncertain candidates remain unselected. A normal purge prints the selected absolute paths and requires a final confirmation that defaults to No. `--dry-run` still opens the multi-select, then previews the selected entries without the destructive confirmation. A non-TTY invocation skips the TUI and previews all candidates with their default selection status, even without `--dry-run`.

The scanner excludes Git-tracked candidates, including an entire candidate containing a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, zero-size candidates, global Xcode `DerivedData`, non-Composer `vendor`, and `bin` outside `.NET` projects. Generic artifact names require enclosing project evidence. Never infer that an agent worktree itself is disposable; only discovered artifact directories inside it are candidates. Permanent deletion is best-effort: if it fails partway through, some contents may already be gone; report the failure, continue with later selections, and treat exit status 1 as incomplete cleanup.

## Serve a Local Model Proxy

Use `hgc service model-proxy start` when a local client needs both OpenAI Responses and Chat Completions interfaces backed by provider-level configuration. The default config path is `<workspace>/hagency-model-proxy.toml`; use `--config` instead of `--root` for an explicit file. Only loopback IP addresses are accepted.

```sh
hgc service model-proxy start -r <workspace>
hgc service model-proxy stop -r <workspace>
hgc service model-proxy restart --config <path> --host 127.0.0.1 --port 8765
```

`start` and `restart` return after the background worker has bound its port. Use the same resolved config path for `stop`; stopping an already stopped worker is successful and reports that it is not running. Linux uses a detached session, while Windows uses a detached no-console process group. Lifecycle state and logs use `XDG_STATE_HOME`/`~/.local/state` on Linux and `LOCALAPPDATA` on Windows; an absolute `HAGENCY_STATE_HOME` overrides the state root. Logs rotate at 10 MiB with three backups. Read the log path printed by `start` before diagnosing a startup or runtime failure.

Configure each provider through a provider adapter. Each configured provider still has exactly one native protocol. Do not add model lists, aliases, provider prefixes inside `model`, or model-based routing rules.

```toml
version = 1
default_provider = "openai"

[providers.openai]
adapter = "openai"
api_key = { env = "OPENAI_API_KEY" }
```

Environment-backed values load from `.env` beside `hagency-model-proxy.toml`; process environment values override the file. Hooks receive the merged values through the read-only `init.env` mapping. Do not commit `.env`.

The `openai` adapter supplies the Responses protocol and API root. Use `openai_compatible` with a `base_url` for compatible providers; it defaults to Chat Completions. Override `protocol` only at provider level. New built-in provider families belong in `tools/hagency-cli/src/hagency_cli/model_proxy/providers/<adapter>.py` and export one `ProviderAdapter` value named `ADAPTER`; the filename is discovered directly, so no registry edit is required. Keep deployment secrets and tenant-specific values in config, and keep model routing out of adapters.

Use `http://127.0.0.1:8765/v1` for the default provider and `http://127.0.0.1:8765/<provider>/v1` for an explicit provider. Both base URLs expose `/responses`, `/chat/completions`, and `GET /models`. Matching protocol traffic keeps request/response entity bytes intact except where an explicitly configured body or SSE hook changes them; cross-protocol traffic uses the built-in bridge. `/models` proxies the adapter's model-list path through the normal Hook pipeline. Native resource subpaths are passed through only to a matching provider protocol.

Credential-like downstream headers are removed unless named in `forward_credential_headers`. Prefer static or environment-backed provider headers. For custom provider authentication or wire-shape adjustments, set `hook = "name.py"` and place the file under `<config-dir>/hooks/`. It must export `Hook`, accept `HookInit`, and may define these async methods:

```python
from hagency_cli.model_proxy import AuthPatch, HeaderPatch


class Hook:
    def __init__(self, init):
        self.options = init.options
        self.token = init.env["CORP_TOKEN"]

    async def authenticate(self, ctx, request):
        token = await obtain_provider_token(self.options, self.token, request.body)
        return AuthPatch(headers=HeaderPatch(set=(("Authorization", f"Bearer {token}"),)))
```

`prepare_request` and `process_response` see provider-native data; `authenticate` sees the final body and may return only header/query patches. A Hook may implement `fetch_models(ctx)` and return `list[str]` when the provider does not expose a standard model-list operation; synthesized records use `created = 0` because this compact contract has no creation metadata. Missing methods preserve the relevant raw path. Defining `process_response` buffers each non-SSE response before the hook runs and applies a 64 MiB response limit, so omit it for authentication-only Hooks. Hook loading and contract validation happen before listening, hook failures are fail-closed, and files are not hot-reloaded. Treat Hook files as trusted in-process code.

## Safety and Boundaries

- Prefer `--dry-run` before commands that mutate checkouts, profile configs, source configs, files, symlinks, or copied skill directories.
- Treat `file purge` as permanent deletion: inspect its preview and exact absolute-path confirmation before approving cleanup.
- Do not expose the model proxy through a separate port forward or public listener without adding an appropriate downstream authentication layer.
- Do not create `agents/openai.yaml` for this repo-local skill unless the user explicitly asks for it.
