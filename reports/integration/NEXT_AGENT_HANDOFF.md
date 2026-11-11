# 恢复入口

当前仅 [W3 #10](https://github.com/SKYdayDUCK/NovelFactory/pull/10)。先核 GitHub 最新 main/heads、PR 目标和 CI；对照 [当前阶段](CURRENT_STAGE.md)、[交付台账](DELIVERY_LEDGER.md)、[矩阵](PR_MERGE_MATRIX.md)、[已知失败](KNOWN_FAILURES.md)。NF Bridge 已在 `15f606c99c765deeffd904ad540441eb6476b3af` 签收，真实 WAL/角色/负载原件见 [签收](NF_Bridge_主线签收.md)。保留全部原提交，禁止强推和跳门禁。

W3 合并并复验后依次推进 W4 #12、W5 #14、W6 #15 和 P5.6 主线全系统验收。之后独立集成 P6 #17 共享预算、#16 预检与部署，归档 #18 的适用证据。真实本地创作状态为 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。《潮闸余灯》为六场景 TEST_FIXTURE。

P5.7 依次交付 S0 指令与加载审计、S1 细粒度审核、S2 运行时角色与 Skills 指令、S3 默认关闭的动态实例与版本、S4 冻结样本对照与候选晋级/回滚、S5 双模式全系统和观测验收。每一阶段均须通过 PR、合并、再验和签收。最终回归后保留 Scion #4 为唯一开放 PR，Issue #13 继续跟踪未达标容量。
