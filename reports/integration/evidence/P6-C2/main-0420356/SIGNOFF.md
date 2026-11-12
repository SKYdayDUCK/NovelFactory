# P6 本地预检、部署及作者交接签收

**MERGED_AND_VERIFIED**。[#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) 于 2026-11-12 04:08:54 UTC 合并为 `0420356b0bd0bfd675494e581799946c4b967f6e`。基线是含共享预算的 `533c1f526ef8841610d5c414e77ecbea8bbd5e35`，候选是 `6c01eee41f0366dd9a136038161b2d030d07ab67`。候选、PR 合成提交和实际主线的树均为 `c15a497115b404e05a1abc194363888c3ca66725`。保留的旧堆叠头为 `bc7824078a7613c5e1a0d26031c3c64fa2db8490`。[源码与历史原件校验](../source-lineage.json)。

| 验收范围 | 合并主线检查 |
| --- | --- |
| 预检与现场授权 | 15 组输入中 5 组匹配、10 组错误被拒。空模板由真实 CLI 返回阻断，0 场景、0 数据库连接、0 模型请求、实际成本 null。对已保存的真实预算回执单独验算，生产服务仍于动作时重新校验。 |
| 实际场景、知识与任务 | 在真实 PG17 和 Redis 上建立采用过的卷计划及世界过程，以公开登记入口写入 6 个任务，包含三名演员的冻结 Skill 和各自 BELIEF。重放同一键保持原登记，在原 Redis outbox 查到对应 trace；无支付模型调用或消费。跨作品、旧 WorldRef、非授权 trace 等失败路径均有断言。 |
| 作者使用路径 | 绑定作品与分支后可导出冻结版本。既有文件保全、跨作品及跨分支拒绝、撤权后拒绝均已执行。固定六场样卷正文 6500 UTF-8 字节，A/B 两份表保留五维空白评价。事件和记录时间均来自范围受限的冻结日志。 |
| 双服务和恢复 | 真实 stdio 与两套数据库完成受限访问与结算。P6 恢复演练使用 PostgreSQL 17、Redis 8.0.2 与 WAL，150 Token 总额度中，原 UNKNOWN 100 Token 与 0.5 美元仍占用，剩 50 Token。旧 worker 被围栏拒绝，目标切点事务丢失 0。 |
| 工程与容量 | 七个工作流中 NF、Workshop、P6 离线和 P4 物理恢复通过。NF 主测试 835 项通过、17 项因运行器限制跳过，另有 17 个子测试通过。单元与契约 403 项通过，另有 17 个子测试通过。四个既有压力作业继续低于原线，其中 1000 开环读发生 10 个传输错误。原 AT-30 容量缺口继续 **NOT_MET**。[主线失败数值及判定](qualification.json)。 |
| 真实创作与文学评价 | `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。工程样卷使用 15 次 fixture 调用，真实模型调用 0，收到人工审读 0，文学质量 `NOT_EVALUATED`。 |

[PR 逐项验收](../pr16/qualification.json)、[主线结果](qualification.json)和[原始文件目录与哈希](manifest.json)可用于核对每轮独立运行的身份及原件。pytest 主测试与 JUnit 的子测试分别计数；宿主限制跳过与容量失败逐项保留。开发工作区在 C2 期间中断，最终候选由 GitHub Git 对象重建并验证。正式 PR 与主线均在 GitHub 托管环境实际执行了 PostgreSQL、Redis、双服务、CLI 和恢复测试。

[作者本地手册](https://github.com/SKYdayDUCK/NovelFactory/blob/0420356b0bd0bfd675494e581799946c4b967f6e/docs/P6_本地真实模型创作交接.md) · [部署及故障处置](https://github.com/SKYdayDUCK/NovelFactory/blob/0420356b0bd0bfd675494e581799946c4b967f6e/reports/handoff/P6_DEPLOYMENT.md) · [独立审读空表](https://github.com/SKYdayDUCK/NovelFactory/blob/0420356b0bd0bfd675494e581799946c4b967f6e/reports/handoff/P6_READER_FORM.md)。

下一阶段核查并保全 [#18](https://github.com/SKYdayDUCK/NovelFactory/pull/18)，按实际开放 PR 列表签发 PRE_HARNESS_BASELINE。
