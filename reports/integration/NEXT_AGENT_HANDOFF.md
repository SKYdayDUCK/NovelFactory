# 恢复入口

Workshop W4 [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12)。启动时远端既有分支 `dev/aw-w4`、头 `71b471416c9407e348d4bdce817588def5f11d5b`、原目标 `dev/aw-w3`。正式集成以最新 `main` 为目标。已签收的基线为 [W3 `e6763d844f023d7c6a13060dbaebca0416fe3099`](W3_主线签收.md)。

复用 W4 的技能版本与五类共享、七种审核模式，核验权限回避、版本冻结、配额和历史库升级。先厘清旧堆叠、按当前阶段精确运行真实门禁，通过正常 PR 合并，复验 main 并存档。其他阶段继续冻结。

[交付台账](DELIVERY_LEDGER.md) · [完整矩阵](PR_MERGE_MATRIX.md) · [已知限制](KNOWN_FAILURES.md)。

## W4 托管 CI 恢复

2026-11-11 21:02:59 UTC，#12 候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`、树 `cb601579cceafd8cd33d35664a31f8d9dd38c879`，基线 `e6763d844f023d7c6a13060dbaebca0416fe3099`。GitHub Actions 组件已于 20:48:25 UTC 标为 `major_outage`。[GitHub 平台事件](https://www.githubstatus.com/incidents/dvy0t3mvh1lg) · [该时点的 GitHub 状态、PR、main、作业与部署审批回执](evidence/W4/candidate-3ed83ff/platform-outage-20261111T210259Z/checkpoint.json)。

1. 复查 #12 的 head、main、原 [Workshop CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063)、原 [P1 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977)。此快照两项均 queued、0 jobs、无待部署审批。候选同 SHA 已有 NF 834/834 和双服务专项证据。[NF 全量原件及三次运行](https://github.com/SKYdayDUCK/NovelFactory/blob/5f07125458a0249c3203c9c0d5b03ff92abdcd50/reports/integration/evidence/W4/candidate-3ed83ff/independent/local-nf-full.md) · [双库、124 个候选用例和附加阅读覆盖](https://github.com/SKYdayDUCK/NovelFactory/blob/57f5c3e0b08112330b77d2016465f78587d81ea6/reports/integration/evidence/W4/candidate-3ed83ff/independent/README.md)。
2. 原运行获得 runner 后持续核对作业、实际 checkout、两份 Workshop 工件和 P1 原始 JUnit。若 head、base 或树发生变化，按实际变更重新确认候选依据。等待实际托管成功后按正式 PR 合并。
3. 以 GitHub 产生的真实合并 SHA 再跑新旧库升级、双 MCP、阅读覆盖、Skills 权限、两侧 WAL、Redis 恢复及适用主线 CI，保存主线回执，签收 W4 后才允许 W5。

## 后续串行交付顺序

1. W3 #10 已签收。当前只集成 **W4 #12**，随后依次验收 W5 #14、W6 #15。每个阶段必须完成开发、测试、PR、CI、正式合并、主线复验和签收，之后才能开启下一阶段。W6 签收后执行 P5.6 全系统主线验收，包括实际双 MCP/双 PostgreSQL/Redis、权限与角色认知、历史升级、故障恢复和容量。AT-30 与 Issue #13 保留原指标和未达标结果。
2. P5.6 全验完成后，先集成 **P6 #17 共享预算**，再集成 **#16 预检与运行入口**，逐个合并并在主线复验。随后归档 P6 离线与模拟工程原件，核对 #18 中 P5.5 补充证据的永久合入位置。六场景《潮闸余灯》属 `TEST_FIXTURE`；作者的真实本地创作验收状态为 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。
3. P5.7 以六个独立阶段 PR 交付。S0 审计运行时 Agent/Skills 并验证设计，S1 实现细粒度审核内核，S2 改进实际加载的角色指令与 Skills，S3 实现动态版本、实例及实时授权，S4 实现反馈归因、冻结评测、受控晋级与回滚，S5 完成预算、日志、指标、双模式与全系统验收。总开关 `dynamic_agents.enabled = false` 默认关闭。各阶段保留原始证据；文学质量提升须有真实盲评。
4. 最终执行主线回归，归档 `reports/integration/FINAL_DELIVERY_REPORT.md`。逐项查清存量 PR 的实际合并或等价落点，保留原提交及证据，最终仅 Scion PR #4 开放。长期跟踪的 Issue 可继续开放。


**2026-11-11 21:15 UTC 续办点**。GitHub 再次确认 `main` 为 `e6763d844f023d7c6a13060dbaebca0416fe3099`，#12 未合并，候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`。正式 [P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977) 和 [Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) 均为 queued，作业数均为 0。已核已有“NovelFactory 串行交付续办”处于启用，按小时检查，上一轮于 21:10 UTC 结束，下次计划 22:04 UTC。恢复时先查询这两个运行和最新头；失败则诊断原日志，成功则核验原件、同树和审查，走正常 PR 合并、重验实际 main 并签收。只在 W4 签收后开启 W5。随后 W6 与双服务最终验收须针对真实 main 产出 `reports/integration/P5_P5.5_P5.6_主分支最终验收.md`，再恢复 P6。
