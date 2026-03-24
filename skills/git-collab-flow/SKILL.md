---
name: git-collab-flow
description: manage collaborative git workflows that use dev, feat-*/dev-*, and local-* branches. use when you needs to decide the next git commands for syncing mainline updates, rebasing feature branches, cherry-picking public commits from local work, preparing a clean pr, or separating public and private commits in this contribution model. 也用于多人协作项目中，根据当前分支状态和目标，生成下一步 git 命令、检查步骤、冲突处理和风险提醒。
---

# Git Collab Flow
## Overview
将用户在多人协作项目中的分支状态与目标，转换成一组安全、最小化、可复制执行的 git 命令。

围绕三层分支模型工作：`dev` 跟踪主线，`feat-*`/`dev-*` 作为可公开的 pr 分支，`local-*` 作为包含本地私有提交的工作分支。优先保持 pr 历史干净，并显式区分"可公开提交"和"本地私有提交"。

## Fixed Branch Model
始终按以下约定推理，除非用户明确说明仓库另有约定：
- `dev`：跟踪主线，不在其上堆叠个人功能开发提交。
- `feat-foo`/`dev-foo`：基于 `dev` 的公开功能分支，用于推送到远端并发 pr。
- `local-foo`：`feat-foo` 的本地工作分支，允许包含不想公开的提交、试验性提交、临时修复和整理中的提交。

将 `feat-*`/`dev-*` 视为"可公开历史"，将 `local-*` 视为"可重写、本地优先历史"。

## Decide the User Intent
先判断用户意图，再选择对应工作流：

1. **发布本地公开提交到 `feat-*`**
   用户通常会说"挑几个提交进 pr""把 local 上能公开的同步到 feat""保留私有提交，只发公开部分"。
2. **把主仓库更新同步进当前功能开发**
   用户通常会说"主仓库更新了""同步远端变更""让本地跟上最新 dev / feat"。
3. **让 `feat-*` 跟上 `dev` 并整理 pr**
   用户通常会说"准备 pr""让 pr 更干净""把最新 dev 变基进来"。
4. **解释当前状态或排查下一步**
   用户通常会贴 `git status`、`git branch -vv`、`git log`、冲突信息，要求判断接下来该做什么。

如果意图不完整但已经能合理推断，就直接给出默认安全流程，并清楚写出假设。只有在缺失信息会导致明显误导时，才请求用户补充关键信息。

## Collect the Minimum Context
当上下文不足时，只补齐真正影响命令的关键事实：

- 当前所在分支。
- 目标分支和目标结果。
- `origin/feat-*` 是否可能已有新提交。
- `dev` 是否已经更新。
- 哪些提交需要公开，哪些提交要保留在本地。

如果用户贴了仓库状态，优先基于它推理；不要忽略现成证据。

当需要先检查状态时，优先建议这些命令：

```sh
git status -sb
git branch -vv
git log --graph --decorate --oneline --all -20
```

当需要只查看 `local-*` 相比 `feat-*` 多出的提交时，优先使用：

```sh
git log feat-foo..local-foo --oneline
```

## Response Contract
始终按下面的结构组织回答，除非用户只要一小段命令：

1. **目标**：用一句话复述你认为用户要完成的事。
2. **假设**：列出你为了给出命令所做的关键假设。
3. **命令**：给出按顺序执行的命令块；已知分支名时直接替换，不要保留 `foo` 占位符。
4. **检查点**：说明执行后应如何验证结果。
5. **风险与回退**：指出哪些步骤会改写历史、何时需要 `--force-with-lease`、发生冲突后如何继续或中止。

避免只给抽象建议。优先给用户可以直接复制执行的命令序列。

## Core Rules
严格遵守这些规则：

- 按 `dev -> feat-* -> local-*` 的顺序同步更新。
- 当远端 `feat-*` 可能有更新时，先 `git fetch origin`，再在 `feat-*` 上执行 `git pull --rebase origin feat-foo`。
- 当 `dev` 更新后，优先把最新 `dev` rebase 到 `feat-*` 上，以保持 pr 历史干净；必要时提醒用户随后对远端 `feat-*` 使用 `git push --force-with-lease`。
- 当 `feat-*` 更新后，在 `local-*` 上执行 `git rebase feat-foo`，让本地工作分支重新站到最新公开历史之上。
- 当用户只想公开 `local-*` 中的一部分提交时，不要建议直接 merge `local-*` 到 `feat-*`。先让 `local-*` rebase 到 `feat-*`，再在 `feat-*` 上 cherry-pick 需要公开的提交。
- 只有当用户明确想把多个提交先放进暂存区 / 工作区自行整理时，才建议 `git cherry-pick -n`。
- 如果 `local-*` 上的提交依赖顺序复杂，先帮助用户识别依赖，再决定 cherry-pick 顺序。
- 不要建议对共享的 `dev` 历史做重写操作；默认 `dev` 只从远端拉取更新。
- 如果 `feat-*` 是多人共享分支，明确提醒用户：force push 前先确认没有覆盖别人的工作。
- 不要把 `git push --force` 作为默认命令；默认使用 `git push --force-with-lease`。
- 不要默认建议 `git reset --hard`、`git clean -fd` 这类破坏性命令，除非用户明确要求丢弃修改或进入恢复场景。

## Default Playbooks
以下是一些常见的工作流

### Publish Selected Local Commits to `feat-*`
只把 `local-*` 中适合公开的提交放入 `feat-*`，并保留本地私有提交：
```sh
git fetch origin

git checkout feat-foo
git pull --rebase origin feat-foo

git checkout local-foo
# 日常开发提交已经在 local-foo 上
# commits ...

git rebase feat-foo
git log feat-foo..local-foo --oneline

git checkout feat-foo
git cherry-pick <commit1> <commit2>
# 或选择整段：<old_commit>^..<new_commit>
```

完成后，提醒用户验证提交顺序是否正确，再推送 `feat-*`。

验证：
```sh
git log --oneline --decorate feat-foo -20
git log --oneline origin/feat-foo..feat-foo
```

### Rebase `feat-*` on `dev`
当用户要让 pr 跟上最新主线时：

```sh
git checkout dev
git pull origin dev

git checkout feat-foo
git rebase dev
git push origin feat-foo --force-with-lease
```

完成后，再提醒更新对应的 `local-*`：

```sh
git checkout local-foo
git rebase feat-foo
```

### Sync the Whole Stack
把主仓库更新安全地同步到整条开发链路上：
```sh
git fetch origin

git checkout dev
git pull origin dev

git checkout feat-foo
git pull --rebase origin feat-foo
git rebase dev

git checkout local-foo
git rebase feat-foo
```

如果 `feat-*` 在 rebase `dev` 后历史被改写，补充：
```sh
git checkout feat-foo
git push origin feat-foo --force-with-lease
```

必要时验证：
```sh
git status -sb
git log --graph --decorate --oneline --all -20
```

## Conflict and Recovery Rules
当命令产生冲突时，明确告诉用户下一步：

### Cherry-pick 冲突
```sh
git add .
git cherry-pick --continue
# or
git cherry-pick --abort
```

### Rebase 冲突
```sh
git add .
git rebase --continue
# or
git rebase --abort
```

如果用户预期冲突很多，可以建议先从 `feat-*` 拉一个临时分支，完成 cherry-pick 和冲突处理后再合并 / 快进回 `feat-*`。需要时参考 [references/playbooks.md](references/playbooks.md) 中的"heavy-conflict import branch"部分。

如果用户因为 rebase 或 cherry-pick 改写历史而迷失状态，优先建议查看：

```sh
git reflog --date=local -20
```

然后基于 reflog 给出恢复建议。

## Inspect Before Deciding

当用户只贴一部分状态，先要求或建议补齐以下最小检查：
```sh
git status -sb
git branch -vv
git log --graph --decorate --oneline --all -20
git log feat-foo..local-foo --oneline
```

根据输出，判断：
- `local-*` 是否只是比 `feat-*` 多出几个可公开提交。
- `feat-*` 是否落后于 `origin/feat-*`。
- `feat-*` 是否落后于 `dev`。
- 是否已经处于 rebase / cherry-pick / merge 中间态。

## Review Commit Selection Carefully
当用户要"挑提交"时，优先帮助用户做下面几件事：
- 确认只挑选应公开的提交。
- 指出依赖关系，例如后面的修复提交依赖前面的重构提交。
- 当提交边界不适合直接公开时，先指出"这些提交最好先整理 / squash / 拆分"，再给出最安全的最小操作方案。
- 当用户贴出提交列表时，显式标记"建议公开""建议保留在 local-*""建议整理后再公开"。

## Use Concrete Branch Names
只要用户已经提供真实分支名，就使用真实分支名生成命令，例如：

- `feat-payment` / `local-payment`
- `feat-search` / `local-search`

不要把真实场景重新抽象成 `feat-foo` / `local-foo`，除非用户原本只用了占位名称。

## Example Requests to Match
下面这些请求都应该触发本技能：
- "远端 `feat-payment` 更新了，怎么同步到 `local-payment`？"
- "`dev` 有更新，我想让 pr 更干净，下一步怎么做？"
- "我在 `local-search` 上有 5 个提交，只有 2 个想公开，帮我给出 cherry-pick 流程。"
- "这是我的 `git log --graph` 输出，帮我判断应该 rebase 还是 cherry-pick。"
- "多人协作下，怎么维护 `dev / feat-* / local-*` 这套分支模型？"