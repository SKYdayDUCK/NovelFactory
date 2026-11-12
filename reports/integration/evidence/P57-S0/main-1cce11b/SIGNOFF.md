# P5.7-S0 主线签收

**MERGED_AND_VERIFIED**。PR #28 于 2026-11-12T06:10:06Z 合并，正式主线为 [`1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`](https://github.com/SKYdayDUCK/NovelFactory/commit/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b)。最终候选 `a8073b6664df8953330dd3bc0240038fcb64f09a`、PR 合成提交 `e2f9becfc4e5dbf5182349132ab422ac99ae0fbe`、真实主线的 Git tree 均为 `fa1a10cc5f237d5a06bbac283c1c5e492fdf5ed6`。19 个变更文件覆盖 S0 架构、契约、审计、验证及交接。[候选逐文件比对](../candidate-a8073b6/frozen-tree.json) · [GitHub 合并记录](https://github.com/SKYdayDUCK/NovelFactory/pull/28)。

## 交付与边界

- [技术架构](https://github.com/SKYdayDUCK/NovelFactory/blob/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b/reports/harness/S0_ARCHITECTURE.md)与[接口和数据模型](https://github.com/SKYdayDUCK/NovelFactory/blob/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b/contracts/CREATIVE_HARNESS.md)说明 Core、Workshop、Harness 分工。纯指令晋级或回退时须复核正式分支 HEAD、解释代际、世界事件和在途快照。
- [逐文件审计](https://github.com/SKYdayDUCK/NovelFactory/blob/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b/reports/harness/S0_INSTRUCTION_AUDIT.md)核对 77 份冻结来源，含 34 个必核指令与运行时目录文件。根技能索引已补齐。8 份角色正文所标 2.0.0 与服务目录记录的 2.1.0 分列保存。
- [创作评估协议](https://github.com/SKYdayDUCK/NovelFactory/blob/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b/reports/harness/S0_EVALUATION.md)及[验收矩阵](https://github.com/SKYdayDUCK/NovelFactory/blob/1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b/reports/harness/S0_ACCEPTANCE_MATRIX.md)规定三臂冻结输入、相同模型条件、可比较预算以及失败样本和读者分歧的保留口径。第三组须经过本轮候选优化触发及评估。S0 验收的是设计和可执行契约。三臂系统效果的运行属于 S6。

| 范围 | 分支或 PR | 实际合并主线 |
| --- | --- | --- |
| S0 专项 | [业务分支](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43164309298)与 [PR 合成提交](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43164314487) 均通过 | [主线专项](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43166233158)接受 16 个合法样例，拒绝 33 个反例。额外验证两处缺字段的 WorldRef。四个开关初始均关闭。 |
| 静态运行时 | 对全部可选角色、模式、获准 Skill 实际构造快照 | 10 个角色、12 个 Skill、66 个组合。校验来源路径、版本、原始文本、冻结摘要和服务端拒绝的实例。[原始回执](s0.json) |
| NF 完整性 | [PR 的两个 P1 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43164314485) 通过 | [主线两个 P1 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43166232959)通过。PostgreSQL 17 有 835 个主测试和 17 个子测试通过，17 项因 Linux 容器配置受限跳过，pytest 汇总 1 条 warning。Schema 有 403 个主测试与 17 个子测试通过。两份 JUnit 均无错误、无失败。[原 XML 字节与索引](junit-original-files.json) |
| 原容量与等价性 | [候选 10 个作业](../candidate-a8073b6/pr/capacity.json)有 6 个通过、4 个未达标 | [主线 10 个作业](capacity.json)有 6 个通过、4 个未达标。500 读、600 秒为 12,000/12,000 HTTP 200。同世界 15 组输出等价。一小时持续试验为 NOT_EXECUTED。 |

主线 1000 开环读共 5,000 次请求，1,832 次 HTTP 200、3,149 次 HTTP 429、19 次传输错误。50/200 热点与 1000 开环读沿用原门槛，[Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)保持 **NOT_MET**。原始 GitHub 工作流、作业及 ZIP 标识在[运行清单](workflows.json)。三份 S0 原始 ZIP 的官方 SHA-256 与包内 JSON 已和各次输出逐字节核对，[原件归档](originals.json)。

在主线运行完成后，再次查询仅开放 Scion #4，头为 `4f249d520c921c8d6effee9978f7338b912f1dd0`。[正式门禁快照](signed-gate.json)。`P6_REAL_CREATION=BLOCKED_LOCAL_AUTHORIZATION`，`literary_quality=NOT_EVALUATED`。工程签收未使用真实模型或独立阅读意见作文学质量证明。

后续依照 07:09 用户新委托在指定单分支逐阶段测试、提交 S1–S6，全部验收后合入主线并另做独立审阅。当前实施分支已有 S1 工作者。[二十节原始委托](https://github.com/SKYdayDUCK/NovelFactory/blob/1ef78e6b6a4a22339508de52a854e11a4be65bec/reports/integration/P57_SINGLE_BRANCH_MANDATE.md)。
