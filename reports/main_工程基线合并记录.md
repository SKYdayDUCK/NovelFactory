# main 工程基线合并记录

2026-11-11（北京时间） · [SKYdayDUCK/NovelFactory](https://github.com/SKYdayDUCK/NovelFactory)

## 已进入 main 的提交

| 阶段 | main 提交 | 合入的 PR HEAD |
| --- | --- | --- |
| 接管时的 P0/P1 基线 | `721f155c9cc912cddfdae559afe4f2516d0967f2` | — |
| [PR #2](https://github.com/SKYdayDUCK/NovelFactory/pull/2) · P2 工程闭环 | `499289e8e6bd333e17f40c3d36534fa2c8df2894` | `a9547bd651e177d48c9d0e810362e411f510d62d` |
| [PR #3](https://github.com/SKYdayDUCK/NovelFactory/pull/3) · P3/P4 工程增量 | `3e59c2860fed96f058566d1c2d38bcb3140ebd9b` | `23a61398278c9a13e6450d444d4a6d25a88ef5e0` |

两次均通过 GitHub 连接器标记 Ready，以表中的完整 HEAD 作为 `expected_head_sha`，使用 Merge Commit。逐次回读 PR 的 `merged=true`、`main` 引用和合并提交的两个父节点。#2 合并树为 `62b274b6436540df8aaf7e0527f6485af836499f`。#2 合并后才把 #3 从 `dev/p2-production-loop` 改为以 `main` 为目标。此时 GitHub 的 merge base 是 #2 HEAD；相对该合并基点，#3 有 8 个增量提交和 513 个差异文件。分页读取的全部文件名与本地 P2 HEAD → P3/P4 HEAD 的差集一致，其中 441 个为证据文件。#3 合并树为 `e5dd8cc2c2867ce6cb8d45771303b4e943d78269`，与测试树相同。README 与本文在其后以独立文档提交发布。既有《合并前修复报告》及阶段交接材料中的 Draft 状态对应各自的历史检查点。

合并前，两条 PR 的普通评论、审查提交和审查线程均为空。GitHub 分支接口报告 `main.protected=false`、必需检查列表为空，包含继承项的规则集查询返回空列表。安装级连接器读取管理专用保护详情返回 403。仓库允许 Merge Commit，实际合并由标准合并接口执行。工作流中的容量阈值与失败结果完整保留。

## 工程验收

| 复核对象 | 实际结果 | 可核对记录 |
| --- | --- | --- |
| #2 合并前的工程 CI | 真实 PostgreSQL 17 完整回归 472 项通过，重叠的单元、契约等子集 276 项通过。覆盖权限与任务代际、历史版本、正式事件接受、重复请求回执、备份与恢复。六场短卷以确定性夹具走完内部发布流程。 | [#2 工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42863611743) |
| #2 合入 main | GitHub push 工程工作流成功，完整回归 472 项通过（291.10 秒），重叠子集 276 项通过（7.60 秒）。 | [main 工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42955785867) |
| #3 合并前的工程 CI | PostgreSQL 17 完整回归 631 项通过，重叠子集 380 项通过。物理 PITR 通过，11 个附件文件逐一通过清单校验。 | [#3 工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864582299) · [PITR](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864580346) |
| #3 改换基点后的本轮复跑 | 精确树的 PostgreSQL 17.11 完整回归 631 项全部通过，0 失败、0 错误、0 跳过，用时 324.95 秒。包含 9 个跨卷测试、旧 worker 隔离、版本与权限、幂等回执和备份恢复；JUnit 文件 SHA-256 为 `39bf962e30b6a2f3bb23e7ed0ef0bfe4c4159c9b647533fa76f3a6ce27214c2c`。 | `python -m pytest tests -q`，树 `e5dd8cc2`，使用独立数据库 |
| 已有数据迁移 | 由原 P1 代码建立 0002 数据库并接受一次资源转让，再用合并后 P2 及 #3 精确树升级。34 表升至 37 表，原世界 HEAD 和重复请求回执一致，P2 到 P3/P4 的 37 张表逐表内容哈希相同。 | [仓库迁移脚本](../migrations/versions) |
| #3 改换基点后的真实 PITR | 独立 PostgreSQL 17.11 源集群与恢复集群，执行物理基准备份、`pg_verifybackup` 和 WAL 重放。37 表、19 对象一致，目标提交丢失 0，正确排除目标点后提交 1。旧 worker 被拒，原幂等回执一致，1 笔 UNKNOWN 调用与 0.5 预留预算均保留。隔离小夹具从恢复到服务验证约 1.056 秒。 | [演练脚本](../scripts/pitr_drill.py) · 本轮 `result.json` SHA-256 为 `f15117bb4e85c33430500cf5f5d037eacb70f20035f5c01f5784d20af2c22b05` |
| #3 合入 main | GitHub push 工程工作流成功。完整回归 631 项通过（388.20 秒，1 条警告），重叠子集 380 项通过（12.52 秒）。独立物理 PITR 成功，37 表/19 对象、目标丢失 0，11 个留存文件通过校验。 | [main 工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42956637462) · [main 物理 PITR](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42956637448) |

子集计数与完整回归有重叠。工程结论覆盖仓库中实际执行的检查。物理恢复的规模为隔离测试夹具，生产数据体量、远程对象存储灾难恢复以及生产 RPO/RTO 仍待验证。

## 仍未满足的容量与内容验收

| 场景 | 实际结果或缺口 |
| --- | --- |
| 50/200 Agent 热点 | 普通混合负载已有通过记录。热点首试仍出现失败，开放到达速率低于原目标。配置目标为 1000 次/秒，调用开始速率门槛为 950 次/秒。#2 合并前的独立 200 热点另测得请求头开始发送约 22.52 次/秒。[矩阵](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42863611898) · [独立负载](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42863611810) |
| 500 Agent、600 秒探针 | #3 历史实测中，混合任务有 51/51 正式效果的通过记录。600 秒只读探针有 12000/12000 的通过运行，也出现过 11708/12000、292 个 HTTP 429 的运行，跨次稳定性尚未达到原目标。[规模工作流](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864579931) · [历史测量记录](性能与容量修复对照.md) |
| 1000 Agent 和长稳 | 开放只读要求 5000/5000 个有效响应。#3 HEAD 的该次负载返回 1528 个 HTTP 200、3419 个 HTTP 429 和 53 个传输错误。1000 Agent 混合写入、多小时增长世界和 API 与派生工作者共享资源预算仍待实测。[既有容量缺口](未通过目标与后续工作.md) |
| 真实模型与文学 | 真实模型调用为 0，授权环境 `BLOCKED_ENVIRONMENT`，作者全文评价 `PENDING_AUTHOR`，文学质量 `NOT_EVALUATED`。 |
| #2 合并提交 | 两项容量工作流均为 failure。50/200 普通混合通过，50/200 热点和独立 200 热点失败。[矩阵](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42955785655) · [独立负载](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42955785781) |
| #3 合并提交 · 200 Agent | 普通混合的两次重复均完成 21/21 个正式效果，首试失败为 0。热点两次也完成 21/21，但首试失败分别为 642、658 次。独立热点测量最终仍有 142 次 HTTP 429。两条工作流均为 failure。[矩阵](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42956637463) · [独立负载](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42956637449) |
| #3 合并提交 · 500/1000 Agent | 500 混合完成 51/51 个正式效果，首试失败为 0。500 Agent 的 600 秒只读探针本次通过，12000/12000 个 HTTP 200。1000 开放只读的 5000 次中有 1567 个有效 HTTP 200、3377 个 HTTP 429 和 56 个传输错误，实测请求头开始发送 48.64 次/秒。规模工作流总体为 failure。[规模实测](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42956637434) |

## Scion 实验及历史成果保全

[PR #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4) 保持 open、Draft、`merged=false`。实验分支 `experiment/scion-incremental-20261111` 的 HEAD 为 `4f249d520c921c8d6effee9978f7338b912f1dd0`。本轮核对 [S1–S5 已发布快照](https://github.com/SKYdayDUCK/NovelFactory/tree/4f249d520c921c8d6effee9978f7338b912f1dd0/evidence/scion_round2) 全部 79 个文件的 Git blob 及长度。S5 清单中的 37 份归档、38 个分片与 SHA-256 相符。阶段验证包含 6 场景 × 10 配置的 PostgreSQL 冻结轨迹比对，保留早期失败与后续 700/700 回归。[S5 工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42954063627) 通过，三条容量工作流失败。

实验分支默认开启派生 worker 的元数据列投影。审核和查询列投影、B2 稀疏 Scope、B3 后缀重放及其 lazy/prime 开关均默认关闭。B1/B2 继续独立审核。[开关实现](https://github.com/SKYdayDUCK/NovelFactory/blob/4f249d520c921c8d6effee9978f7338b912f1dd0/src/novel_factory/application/scion.py)

本轮盘点 17 个本地工作树，发现 4 个有未提交内容。独立保全 Git 对象、引用、索引、补丁、未跟踪文件及实验材料，共 2354 个路径记录、1520 个唯一文件；逐项回读 SHA-256 成功。S5c 提交后另有两次已完成且未达标的复测（1000 开放只读 A 组、500 稳态 F 组）。将其原始归档和对应汇总共 4 个文件保存到[独立恢复提交](https://github.com/SKYdayDUCK/NovelFactory/commit/c019d971656426a5680d95d2c1bc5a35154d42fa)，分支为 `recovery/scion-s5-evidence-20261111`。用 S5c 自带审计脚本从归档复算，已发布快照的 71 个样本和补录后的 73 个样本分别通过完整性校验。公平性原型与审阅草稿保存在本地恢复副本中。

## 回退

1. 只撤销 P3/P4 时，从当前 `main` 建立回退分支，以第一父提交为主线执行 `git revert -m 1 3e59c2860fed96f058566d1c2d38bcb3140ebd9b`。README 如与后续文档提交冲突，按 P2 回退后的实际状态修订，并在本文追加回退记录。在 PR 中核对 `src/`、`migrations/`、`tests/` 与 #2 合并点的差异，复跑 P2 工程回归和已有数据验证。
2. 需要继续恢复 P1 代码时，在上述回退之后，以同样方法撤销 #2 的 `499289e8e6bd333e17f40c3d36534fa2c8df2894`。干净环境中可直接检出起点 `721f155c9cc912cddfdae559afe4f2516d0967f2` 复现原始基线。远程变更走连接器和正常 PR，保留已发布的提交历史。
3. Git 回退只处理仓库内容。数据库已升级或产生新事件时，应先保存数据库和对象存储副本，使用隔离恢复演练验证目标点、世界 HEAD、权威事件和回执，再单独安排部署切换。

