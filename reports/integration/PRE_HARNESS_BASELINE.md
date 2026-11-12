# PRE_HARNESS_BASELINE

**正式门禁已于 2026-11-12 通过。** [#27](https://github.com/SKYdayDUCK/NovelFactory/pull/27) 的 main SHA 为 [`276820316129d546b0a13118bcbe283b06507795`](https://github.com/SKYdayDUCK/NovelFactory/commit/276820316129d546b0a13118bcbe283b06507795)。PR 测试、合并与主线复验的文件树相同。主线 JUnit 为 PostgreSQL 835 项通过、17 项跳过，另有 17 个子测试通过；Schema 403 项通过，另有 17 个子测试通过。复验后开放列表中唯一条目为 Scion #4。原 XML、运行、开放清单及签收时间见[不可变门禁签收](https://github.com/SKYdayDUCK/NovelFactory/blob/1b0302a6867c56f5871c0b570477d88dc1ffe0a7/reports/integration/PRE_HARNESS_BASELINE.md)。

## 归档提交前的现场


2026-11-12 04:42:54 UTC，最后一项存量实验 #18 已按证据归档关闭。GitHub Open PR 查询仅返回 [Scion #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)，保留其原分支和头 `4f249d520c921c8d6effee9978f7338b912f1dd0`。这一查询发生在提交本归档 PR 之前。经过生产代码及双服务验收的主线是 `0420356b0bd0bfd675494e581799946c4b967f6e`。[原始列表快照](evidence/S9-PR18/open-after-closure.json)。

| 验收范围 | 交付与边界 |
| --- | --- |
| P5 / P5.5 / Workshop W0–W6 | #5、#6、#7、#8、#9、#11、#10、#12、#14、#15 均已正式并入主线。#25 的 W6 签收与 #26 的 P5.6 全系统签收分别完成。[14 项 GitHub 合并来源和主线祖先](evidence/S9-PR18/pre-harness-merge-chain.json) |
| P5.6 综合验收 | [#26 的权威主线记录](https://github.com/SKYdayDUCK/NovelFactory/blob/3ef5a72d7a65edb76a988132a72c4e82ee0805ee/reports/integration/evidence/P56/main-4e8fc5a/SIGNOFF.md)保留组织与角色、权限和 BELIEF 隔离、Skills、有限返工、真实双服务 MCP、新旧库及 WAL/PITR、10/50/200 员工与实际委托的结果。正确性、权限、集成和恢复通过。 |
| P6 共享预算 | [#17 主线验收](https://github.com/SKYdayDUCK/NovelFactory/blob/fee462dfb6923059512b9797b4707e61eec8e8a3/reports/integration/evidence/P6-C1/main-533c1f5/SIGNOFF.md)覆盖金额/Token 双约束、任务及全局预留、取消和发送的双向锁竞争、UNKNOWN 保留、旧库迁移与跨作品拒绝。 |
| P6 本地预检与部署 | [#16 主线验收](https://github.com/SKYdayDUCK/NovelFactory/blob/a9d77b86c425de3cfe67c1b82996377003ac4200/reports/integration/evidence/P6-C2/main-0420356/SIGNOFF.md)覆盖空模板、15 组有效与错误输入、真实登记 6 个任务、3 名演员、0 个模型尝试，以及两套服务和恢复。NF 主测试 835 项通过、17 项跳过，另有 17 个子测试通过。可按[作者手册](../../docs/P6_本地真实模型创作交接.md)导出冻结正文与独立审读空表。 |
| 真实创作及文学质量 | `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`，`literary_quality = NOT_EVALUATED`。工程样卷和空白审读表的原始字节已核对；没有本地作者实测、供应商账单或已完成的独立文学评分。 |
| 历史实验与容量 | [#18 处置与原件](evidence/S9-PR18/README.md)保留 12 个最终文件、9 次源提交的历史版本、10 份直接实验 ZIP 和 4 份关联原件。[逐文件出处](evidence/S9-PR18/provenance-audit.json)列出每个成员与现主线的字节比对。[#13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)仍开放，原 AT-30 50/200 热点和 1000 开环读继续 **NOT_MET**。[原门槛与观测值](evidence/S9-PR18/issue13-capacity-boundary.json) |

本次归档只增加报告、历史源码和原始结果。历史工作流位于归档子目录。归档 PR 合并后复核其确切 merge SHA、主线 push 和“只有 #4 开放”的结果，记录于[正式门禁签收](https://github.com/SKYdayDUCK/NovelFactory/blob/evidence/serial-delivery/reports/integration/PRE_HARNESS_BASELINE.md)。该签收完成后进入 P5.7-S0。
