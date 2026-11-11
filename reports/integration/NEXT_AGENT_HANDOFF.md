# 恢复入口

W5 已在 `b30e76978830bec4c596c04194d749fd01f67402` 完成正式主线签收。当前仅开放 W6 [#15](https://github.com/SKYdayDUCK/NovelFactory/pull/15)。接管先核远端实际状态，并从已签 W5 接入 W6 专属差异。[W5 主线签收](W5_主线签收.md) · [当前阶段](CURRENT_STAGE.md) · [交付台账](DELIVERY_LEDGER.md) · [合并矩阵](PR_MERGE_MATRIX.md) · [已知问题](KNOWN_FAILURES.md)。

## 后续串行交付顺序

1. 当前仅处理 **W6 #15**。正常合并并在实际 main 复核后，另行执行 P5.6 全系统验收，覆盖双服务、组织与角色认知、Skills 和审核、成本、Redis、双库故障、迁移与 PITR，以及 10/50/200 员工和历史 NF 容量。保留既有阈值与 #13。
2. P5.6 全验完成后，先集成 **P6 #17 共享预算**，再集成 **#16 预检与运行入口**，逐个合并并在主线复验。随后归档 P6 离线与模拟工程原件，核对 #18 中 P5.5 补充证据的永久合入位置。六场景《潮闸余灯》属 `TEST_FIXTURE`；作者的真实本地创作验收状态为 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。
3. P5.7 以六个独立阶段 PR 交付。S0 审计运行时 Agent/Skills 并验证设计，S1 实现细粒度审核内核，S2 改进实际加载的角色指令与 Skills，S3 实现动态版本、实例及实时授权，S4 实现反馈归因、冻结评测、受控晋级与回滚，S5 完成预算、日志、指标、双模式与全系统验收。总开关 `dynamic_agents.enabled = false` 默认关闭。各阶段保留原始证据；文学质量提升须有真实盲评。
4. 最终执行主线回归，归档 `reports/integration/FINAL_DELIVERY_REPORT.md`。逐项查清存量 PR 的实际合并或等价落点，保留原提交及证据，最终仅 Scion PR #4 开放。长期跟踪的 Issue 可继续开放。

[历次 W4 事故和 W5 合并前检查点的历史交接](https://github.com/SKYdayDUCK/NovelFactory/blob/d831b0a53cffa12551fe785ad45b756136ffb999/reports/integration/NEXT_AGENT_HANDOFF.md)。

W5 主线签收 b30e76978830，当前只有 W6 #15。核实集成候选的八条部署升级、四个预设、两个受限 SSH 代理、双库恢复与十/五十/二百员工；提交、CI、实际合并、同 main 重跑后再签收。随后独立进行 P5.6 主线全系统验收。

W6 #15 的当前正式候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`，树 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。GitHub 预合并 a1f1021a 与该候选同树，目标 W5 main 为 b30e7697。归档 [当前正式工作流](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43118072846) 和另外五个运行、同 SHA 原生完整 PostgreSQL，再按 Issue #13 逐项匹配历史未达门槛。确认精确 head/base 后正式合并并在新 main 重新验证。
