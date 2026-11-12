# NovelFactory 接管记录

## 远端与本地

2026-11-11 23:24（北京时间）读取远端 PR 与分支。首个集成目标为 #6。main 为 `a8f37b72286163e9117f8874a0e175c61204ff8b`，P5 #5 已于 13:50:15 UTC 合并。#6 源码 `97eeceac266016b0d5da62786b019b704bfcdb70`，相对 main 左右提交数 0/29，差 257 个文件。#21、#24 已是其祖先。完整 PR 清单区分 head、base、正式 merge SHA、Draft、审批、评论、审查线程和当时的 Actions；#13 是 issue。可合并的图判定与正式合并时间分别保存。[PR 快照](evidence/github-takeover.json) · [远端分支及 SHA](evidence/remote-refs.txt)

| 恢复位置 | 实际 Git 状态 | 保全 |
|---|---|---|
| 新接管工作区 | 初始无 Git；单独克隆全部远端分支，跟踪 #6 | 与原工作树隔离 |
| 早期 P0/P1 仓库与 detached review worktree | 分别为 `838a50e4866d3e1d35b1eec78e36994a3d8606c6`、`b02ffa77c7f0eed5a88d62a9be1bfe84e4630d60`；两个工作树干净，stash 为空 | 在接管工作区 recovery/p0-legacy 下另存全部 refs/bundle 和无引用 blob；核验 bundle 及 blob 原始 Git 哈希。早期原仓完整保留 |
| 上一运行的 work / release-guard-verify | 本地预演 `a682c7abeef8e922abbc2077210fd1fe790c3c7e`，另一工作树 `1d950705`；初查均干净，无 stash | 预演两父为 `1d950705ccdebaca02171bd24293682f6efeab54` 和 `43dc369ab73e2faacb509b6ca16995848fc92c28`；GitHub Git object 接口返回 404。在新的完整 clone 中从增量 bundle fetch 回原始提交，远端各原父分支保留。后续阶段不取此混合预演作为集成提交。[可恢复的 Git 对象](evidence/p56-local-rehearsal.bundle) |
| p55-final / p5-late/nf / p5-late/bridge | 分别为 `97eeceac`、`43dc369a`、`90cac947`；初查 Git 干净、无 stash。已阅本地分支、远端 refs、worktree 和 reflog | 保留原目录、分支及其历史。待运行结束单独确认产生的原始回执 |

## 冻结

#7、#8、#9、#10、#12、#14、#15、#17 通过 GitHub 转为 Draft。#11、#16、#18、#20、#22、#23 与独立 #4 本已是 Draft。#19 保留关闭记录。仅 #6 进入本轮集成。源代码分支没有因为暂停而删除、重写或重新播放。

接管期间另一个工作区仍有写入。#15 后来由 a0d8c7e 更新至 ac579e0b89b55e613d32750ce1c07210e0b07022，仍为 Draft。本文的远端表是时间点快照。轮到任何阶段前重新拉取，不以冻结标记推断分支无人写入。
