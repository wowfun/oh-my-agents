# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Hagency CLI

`hgc` manages skill sources, installation, profiles, project file sync, artifact cleanup, and a local model proxy. From the repository root:

```sh
uv tool install -e tools/hagency-cli
hgc --help
hgc skill list --source workspace
hgc skill add workspace:skills/analyze-diff --dry-run
hgc skill add workspace:skills/analyze-diff
```

Skill installation defaults to the invocation directory's `.agents/skills`; use `--dir /path/to/project` for another project or `--global` for `~/.agents/skills`. Workspace discovery checks `--root`, current-directory ancestors, then the editable-installed Hagency Kit checkout. Wheel installations require `--root` outside a workspace.

Read `hgc COMMAND --help` and `hgc COMMAND SUBCOMMAND --help` for complete operation instructions. The [Hagency CLI guide](tools/hagency-cli/README.md) covers configuration, sources and profiles, SFTP and offline transfers, cleanup, the model proxy, troubleshooting, and development. Install shell completion with `hgc --install-completion`.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

A profile lists the source names and skill selectors it enables in `profiles/<name>/config.toml`. After applying a profile, selected skills are materialized in the requested skills container; `-d/--dir` uses the workspace's `.agents/skills` container.
