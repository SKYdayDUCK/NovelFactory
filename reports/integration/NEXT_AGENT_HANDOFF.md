# 恢复入口

1. 读取 [CURRENT_STAGE](CURRENT_STAGE.md)、[交付台账](DELIVERY_LEDGER.md)、[PR 矩阵](PR_MERGE_MATRIX.md)；从远端 PR 的 head/base、merge 时间和最新回执确认当前唯一阶段。
2. 拉取全部远端引用并核对 worktree、已有提交、未提交工作和 stash。任何候选变化都重新审阅差异，保留现有工作。
3. 使用 [源码和测试索引](01_现有功能及证据清单.md)、[CI/实验](02_CI失败与合并阻断分类.md) 与 [顺序](03_PR依赖和集成顺序.md)。P5.5 的完整测试使用 PG17.11、Redis8.0.2、Codex 0.167.0-alpha.4；CI 的单元、生产完整性、物理 PITR、桌面及负载运行各有独立范围。
4. 所有阶段使用单一活跃 PR；验收后经 PR 合并，验证 main，再保存回执，最后才激活下个阶段。保持所有实验分支、原始负载及阈值。

P5.5 的正式主线已核对为 `df1e0cc904e3b94a360d86e3a3e00bfec3258673`。[实际主线回执](p55-main/README.md)。仅 W0 开放。
