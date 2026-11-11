# 恢复入口

当前仅 [W3 #10](https://github.com/SKYdayDUCK/NovelFactory/pull/10)。先核 GitHub 最新 main/heads、PR 目标和 CI；对照 [当前阶段](CURRENT_STAGE.md)、[交付台账](DELIVERY_LEDGER.md)、[矩阵](PR_MERGE_MATRIX.md)、[已知失败](KNOWN_FAILURES.md)。NF Bridge 已在 `15f606c99c765deeffd904ad540441eb6476b3af` 签收，真实 WAL/角色/负载原件见 [签收](NF_Bridge_主线签收.md)。保留全部原提交，禁止强推和跳门禁。

## 后续串行交付顺序

1. 当前只集成 **W3 #10**。随后依次验收 W4 #12、W5 #14、W6 #15。每个阶段必须完成开发、测试、PR、CI、正式合并、主线复验和签收，之后才能开启下一阶段。W6 签收后执行 P5.6 全系统主线验收，包括实际双 MCP/双 PostgreSQL/Redis、权限与角色认知、历史升级、故障恢复和容量。AT-30 与 Issue #13 保留原指标和未达标结果。
2. P5.6 全验完成后，先集成 **P6 #17 共享预算**，再集成 **#16 预检与运行入口**，逐个合并并在主线复验。随后归档 P6 离线与模拟工程原件，核对 #18 中 P5.5 补充证据的永久合入位置。六场景《潮闸余灯》属 `TEST_FIXTURE`；作者的真实本地创作验收状态为 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。
3. P5.7 以六个独立阶段 PR 交付。S0 审计运行时 Agent/Skills 并验证设计，S1 实现细粒度审核内核，S2 改进实际加载的角色指令与 Skills，S3 实现动态版本、实例及实时授权，S4 实现反馈归因、冻结评测、受控晋级与回滚，S5 完成预算、日志、指标、双模式与全系统验收。总开关 `dynamic_agents.enabled = false` 默认关闭。各阶段保留原始证据；文学质量提升须有真实盲评。
4. 最终执行主线回归，归档 `reports/integration/FINAL_DELIVERY_REPORT.md`。逐项查清存量 PR 的实际合并或等价落点，保留原提交及证据，最终仅 Scion PR #4 开放。长期跟踪的 Issue 可继续开放。
