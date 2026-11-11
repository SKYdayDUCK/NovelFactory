# 恢复入口

当前只开放 W5 [#14](https://github.com/SKYdayDUCK/NovelFactory/pull/14)。W4 的实际主线基线为 `6c711adf794842bdf4469ab51e89093d37761bcc`。先核远端最新 main、#14 状态、旧分支和实际差异。保留原始工件，逐阶段独立验证并签收。[W4 主线签收](W4_主线签收.md) · [当前阶段](CURRENT_STAGE.md) · [台账](DELIVERY_LEDGER.md) · [矩阵](PR_MERGE_MATRIX.md) · [已知问题](KNOWN_FAILURES.md)。

## 后续串行交付顺序

1. W4 #12 已签收。当前只集成 **W5 #14**，随后为 W6 #15。每个阶段完成独立开发、测试、PR、托管 CI、正常合并与主线复验后签收。W6 验收完成后，在真实 main 开展 P5.6 双服务、人员组织、角色认知、Skills/审核、成本、迁移/PITR 和 10/50/200、历史 NF 容量验收。保留原 AT-30 门槛及 #13。
2. P5.6 全验完成后，先集成 **P6 #17 共享预算**，再集成 **#16 预检与运行入口**，逐个合并并在主线复验。随后归档 P6 离线与模拟工程原件，核对 #18 中 P5.5 补充证据的永久合入位置。六场景《潮闸余灯》属 `TEST_FIXTURE`；作者的真实本地创作验收状态为 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。
3. P5.7 以六个独立阶段 PR 交付。S0 审计运行时 Agent/Skills 并验证设计，S1 实现细粒度审核内核，S2 改进实际加载的角色指令与 Skills，S3 实现动态版本、实例及实时授权，S4 实现反馈归因、冻结评测、受控晋级与回滚，S5 完成预算、日志、指标、双模式与全系统验收。总开关 `dynamic_agents.enabled = false` 默认关闭。各阶段保留原始证据；文学质量提升须有真实盲评。
4. 最终执行主线回归，归档 `reports/integration/FINAL_DELIVERY_REPORT.md`。逐项查清存量 PR 的实际合并或等价落点，保留原提交及证据，最终仅 Scion PR #4 开放。长期跟踪的 Issue 可继续开放。


**2026-11-11 21:15 UTC 续办点**。GitHub 再次确认 `main` 为 `e6763d844f023d7c6a13060dbaebca0416fe3099`，#12 未合并，候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`。正式 [P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977) 和 [Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) 均为 queued，作业数均为 0。已核已有“NovelFactory 串行交付续办”处于启用，按小时检查，上一轮于 21:10 UTC 结束，下次计划 22:04 UTC。恢复时先查询这两个运行和最新头；失败则诊断原日志，成功则核验原件、同树和审查，走正常 PR 合并、重验实际 main 并签收。只在 W4 签收后开启 W5。随后 W6 与双服务最终验收须针对真实 main 产出 `reports/integration/P5_P5.5_P5.6_主分支最终验收.md`，再恢复 P6。
