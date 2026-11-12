# P5.6 全系统综合验收签收

**MERGED_AND_VERIFIED。** [PR #26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) 于 2026-11-12 02:36:05 UTC 合并至 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc`。其父提交为 #25 的合并点 `ba892d3a424be39efccb697232a5614b26609cc4` 与候选 `e5f5348ca07dc50c9fe52ab271a733ac31fa063c`。合并、候选及 PR 合成检出 `fea6519f7d5d24b8f391648f26fee2ce794035ac` 三者 tree 均为 `515ade557fc82c4ab1a5b67218539a030d0e18cc`。[合并记录](merge.json) · [主线验收报告与复验步骤](https://github.com/SKYdayDUCK/NovelFactory/blob/4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc/reports/integration/P56_SYSTEM_ACCEPTANCE.md)。

| 维度 | 结果 | 精确主线或独立实测依据 |
|---|---|---|
| 正确性 | PASS | [NF push](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43142553579)的 schema/单元 398 项通过，PostgreSQL 818 项通过，17 项托管宿主跳过。独立本地主线另跑 10 项正文、9 项工件核验器、3 项双 MCP 测试，22/22 通过。 |
| 权限与安全 | PASS | [Workshop push](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43142553706)核验两独立服务、集群 ID、OS 身份、跨库和跨 binding 拒绝、员工变更、独立审核和 Skills 冻结与撤回。底层 Linux 宿主 17 项按前一相同运行时代码的原生回执追踪，托管 JUnit 保留为跳过。 |
| 故障恢复 | PASS | Redis 丢失与 SQL outbox 重建、网络丢回执、重复消息、UNKNOWN、两处 WAL 目标错位与双向核对，本地独立 NF pg_basebackup/pg_verifybackup/强制终止/Redis 恢复通过。工程夹具与物理拓扑见主线验收报告。 |
| 工程集成 | PASS | 四种真实组织预设、三档消息、九种委托的工件哈希及 job/attempt/checkout 通过 [push 工件核对](workshop-verified.json)。本地另测 8 条带数据的历史迁移。 |
| 历史容量 | NOT_MET | 三个主线工作流中的四个高压作业仍未达到原条件。500 混合与 500 持续读通过。详见[逐档原始测量摘要](capacity-measurements.json)及 [Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)。 |
| 文学质量 | NOT_EVALUATED | 合成提供者仅用于工程与费用计算验证；独立读者审读与真实创作模型待作者本地授权。 |

[主线五个工作流与逐作业结果](main-qualification.json) · [主线本地验收](independent-main-tests.json) · [候选与主线托管原件](../hosted-b-evidence.zip) · [SHA-256 清单及来源](../hosted-b-summary.json) · [独立基线 94 个原件](../baseline-ba892d3/local-evidence.zip)。夹具保留原始失败、修正和环境限制。P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION。

下一唯一正式交付为 #17 全局 Token 预算。
