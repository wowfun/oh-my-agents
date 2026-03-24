# Playbooks
## heavy-conflict import branch
当预计 cherry-pick 冲突很多时，在一个临时分支上集中处理，再回到 `feat-*`。

- 只有当用户能接受额外临时分支时，才建议这条路径。
- 如果用户明确强调“pr 历史越干净越好”，优先保持快进或重新整理后的线性历史。

### sequence
```sh
git checkout feat-foo
git checkout -b fix-conflicts

git cherry-pick -x <commit1> <commit2>
# 或选择连续区间：<old_commit>^..<new_commit>

# 冲突解决后
git add .
git cherry-pick --continue
```

完成导入后，再把结果带回 `feat-*`：

```sh
git checkout feat-foo
git merge --ff-only fix-conflicts
# 如果无法快进，再根据实际历史决定是否普通 merge
```