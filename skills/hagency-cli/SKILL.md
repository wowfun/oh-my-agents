---
name: hagency-cli
description: Use the Hagency Kit CLI for workspace, source, skill, profile, project artifact purge, and local model-proxy workflows. Trigger for `hgc`, source syncs, skill discovery or installation, profile skill edits, profile initialization, project artifact cleanup, `hagency-config.toml`, `hagency-model-proxy.toml`, provider-level OpenAI Responses or Chat Completions proxying, generated profile skill outputs, and updates to `skills/hagency-cli/SKILL.md`.
---

# Hagency CLI

Use the repo-local `hgc` CLI to inspect and manage Hagency workspaces, sources, skills, profiles, generated profile skill links, rebuildable project artifacts, and the loopback model proxy. If the CLI cannot satisfy the user's request, explain the gap and ask whether to improve `hagency-cli`.

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

Sync remote sources before relying on profile initialization or skill-name inference. For profile-scoped sync, keep the long `--profile` option because `source sync -s` is already the slice selector. Use `--depth` for shallow checkouts and `-s` with 1-based indexes to resume a failed subset.

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

## Install One Skill

Use `skill add` with a unique discovered skill name or an exact `SOURCE:selector`. The default destination is the invocation directory's `.agents/skills`. Use `-p/--path` for an exact skills container, `-d/--dir` for a workspace whose destination is `<workspace>/.agents/skills`, or `--global` for the current user's `~/.agents/skills`. These three options are mutually exclusive. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. `-r/--root` and `--checkout-dir` change source discovery only, never the destination.

```sh
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace> -r <root>
hgc skill add <skill> --global -r <root>
hgc skill add <skill> --dry-run
```

The command installs exactly one skill. If a name is ambiguous, use one of the exact references shown in the error. Source-only and multi-match references are rejected. Installation uses symlinks except on Windows, where it uses junctions.

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

## Initialize Profile Skills

Use `p init` to materialize profile-selected skills. The command requires exactly one destination option: `-p/--path` names the final skills container and appends only each skill name, while `-d/--dir` names a workspace root and writes to `<workspace>/.agents/skills`. Relative destinations resolve against the invocation directory, and `~` expands to the current user's home. These rules are independent of `-r/--root` and `--checkout-dir`, which only control profile and source discovery. Symlinks are the default except on Windows, where junctions are the default. Use `-cp` when the target should get independent copies that can evolve separately from the source.

```sh
hgc p init -p <xxx>/skills <profile> -r <root>
hgc p init -d <workspace> <profile> -r <root>
hgc p init -p <xxx>/skills <profile> -r <root> -cp
hgc p init -d <windows-workspace> <profile> -r <windows-root>
hgc p init -d <git-bash-workspace> <profile> -r <git-bash-root>
```

This is a breaking change to `-p`: migrate `hgc p init -p <root> <profile>` to `hgc p init -d <root> <profile>`. The CLI does not append `.agents/skills` to a `--path` value; use `--dir` when the input is a workspace root.

When multiple discovered directories have the same skill name, an interactive terminal asks which source path to install for that invocation without changing the profile. Non-interactive installation fails instead of guessing; rerun it in a terminal or narrow the profile's `include` selector. A non-interactive `--dry-run` lists all conflicting source candidates and continues the preview without choosing one.

## Command Completion

Use `hgc --install-completion` to install completion for the current shell, or `hgc --show-completion <shell>` to inspect the generated script. Completion includes command aliases and local source, profile, skill, selector, and directory values. It is read-only and silently returns no dynamic candidates when workspace data is missing, invalid, unreadable, or unsynced.

## Purge Project Artifacts

Use `hgc space purge` to remove rebuildable dependency directories, build output, tool caches, and valid `CACHEDIR.TAG` directories from projects. Always preview first because selected artifacts are permanently deleted rather than moved to Trash or the Recycle Bin.

```sh
hgc space purge --dry-run
hgc space purge
hgc space purge <path>...
hgc space purge --paths
```

Positional paths are temporary scan roots and replace both the saved path list and automatic discovery for that invocation. With no positional paths, a nonempty per-user `space-purge-paths` file replaces automatic discovery; a missing or effectively empty file restores it. `--paths` is an exclusive configuration mode: it creates the commented path file when needed, reports current root status, opens `$VISUAL`, `$EDITOR`, `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere, then reloads the result. Entries must be absolute or `~` paths, one per line; blanks and `#` comments are ignored.

The config file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows, with `~/.config/hagency/space-purge-paths` as the Windows fallback. Automatic discovery uses the standard home project roots, `~/.codex/worktrees`, `~/.claude/worktrees`, and eligible direct home-directory project containers. It does not automatically enter system or cloud-storage roots.

In an interactive TTY, Questionary preselects only candidates whose last artifact activity is strictly more than seven days old. Recent and uncertain candidates remain unselected. A normal purge prints the selected absolute paths and requires a final confirmation that defaults to No. `--dry-run` still opens the multi-select, then previews the selected entries without the destructive confirmation. A non-TTY invocation skips the TUI and previews all candidates with their default selection status, even without `--dry-run`.

The scanner excludes Git-tracked candidates, including an entire candidate containing a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, zero-size candidates, global Xcode `DerivedData`, non-Composer `vendor`, and `bin` outside `.NET` projects. Generic artifact names require enclosing project evidence. Never infer that an agent worktree itself is disposable; only discovered artifact directories inside it are candidates. Permanent deletion is best-effort: if it fails partway through, some contents may already be gone; report the failure, continue with later selections, and treat exit status 1 as incomplete cleanup.

## Serve a Local Model Proxy

Use `hgc serve start --model-proxy` when a local client needs both OpenAI Responses and Chat Completions interfaces backed by provider-level configuration. The default config path is `<workspace>/hagency-model-proxy.toml`; use `--config` instead of `--root` for an explicit file. Only loopback IP addresses are accepted.

```sh
hgc serve start --model-proxy -r <workspace>
hgc serve stop --model-proxy -r <workspace>
hgc serve restart --model-proxy --config <path> --host 127.0.0.1 --port 8765
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
- Treat `space purge` as permanent deletion: inspect its preview and exact absolute-path confirmation before approving cleanup.
- Do not expose the model proxy through a separate port forward or public listener without adding an appropriate downstream authentication layer.
- Do not create `agents/openai.yaml` for this repo-local skill unless the user explicitly asks for it.
