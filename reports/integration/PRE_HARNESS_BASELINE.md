# PRE_HARNESS_BASELINE

**MERGED_AND_VERIFIED** · 2026-11-12 05:27:49 UTC。[#27](https://github.com/SKYdayDUCK/NovelFactory/pull/27) 已于 2026-11-12T05:13:37Z 合入 `main`，最终 SHA 为 [`276820316129d546b0a13118bcbe283b06507795`](https://github.com/SKYdayDUCK/NovelFactory/commit/276820316129d546b0a13118bcbe283b06507795)。合并后 GitHub 复验已通过。正式查询开放列表仅返回 [#4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)，头为 `4f249d520c921c8d6effee9978f7338b912f1dd0`，基线分支为 `dev/p3-p4-continuity`。[最终开放列表与门禁](./evidence/PRE-HARNESS/main-2768203/final-gate.json)。

| 阶段 | 接收结果与精确来源 |
| --- | --- |
| W6 [#25](https://github.com/SKYdayDUCK/NovelFactory/pull/25) | 主线 `ba892d3a424be39efccb697232a5614b26609cc4`。[工作流、PITR 与签收](https://github.com/SKYdayDUCK/NovelFactory/tree/9eb7bb52e422f1a8e33a91fe01a1e67fef6375d3/reports/integration/evidence/W6/signoff-ba892d3) |
| P5.6 综合 [#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) | 主线 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc`。双 PostgreSQL、Redis、stdio MCP、角色隔离、实际委托与恢复通过。[系统签收](https://github.com/SKYdayDUCK/NovelFactory/blob/3ef5a72d7a65edb76a988132a72c4e82ee0805ee/reports/integration/evidence/P56/main-4e8fc5a/SIGNOFF.md) |
| P6 共享预算 [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) | 主线 `533c1f526ef8841610d5c414e77ecbea8bbd5e35`。双重额度、并发、UNKNOWN、取消、升级与真实恢复通过。[预算签收](https://github.com/SKYdayDUCK/NovelFactory/blob/fee462dfb6923059512b9797b4707e61eec8e8a3/reports/integration/evidence/P6-C1/main-533c1f5/SIGNOFF.md) |
| P6 本地预检、部署 [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) | 功能基线 `0420356b0bd0bfd675494e581799946c4b967f6e`。工程预检、真实服务登记和授权导出通过。[部署签收](https://github.com/SKYdayDUCK/NovelFactory/blob/a9d77b86c425de3cfe67c1b82996377003ac4200/reports/integration/evidence/P6-C2/main-0420356/SIGNOFF.md) · [作者本地手册](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/docs/P6_本地真实模型创作交接.md) |
| 历史清理 [#18](https://github.com/SKYdayDUCK/NovelFactory/pull/18) | 在 2026-11-12T04:42:53Z 归档关闭。9 个提交、12 个最终文件的源码，10 份直接 ZIP 及 4 份上下游原件均可查。缓存 SQL 方案只在实验分支，已采用的产品功能已核对主线祖先。[原件与逐项采用理由](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/reports/integration/evidence/S9-PR18/README.md) · [逐文件核对](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/reports/integration/evidence/S9-PR18/provenance-audit.json) |

## 归档合并复验

候选 `5aa9b71b66c7800f58214fa368e22ebeb9c3d56b`、PR 测试合成提交 `3ada28b0c35e59dfbcc903f981403fb0e0467dfe`、合并主线 `276820316129d546b0a13118bcbe283b06507795` 三者的文件树均为 `f8b5f9ff2f53d726cf5c666aea7cacf176f1fac9`。相对 [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) 已签收的主线，51 个变更路径全部位于 `reports/integration/`。[文件和 Git 对象比对](./evidence/PRE-HARNESS/main-2768203/frozen-tree.json)。

| 实测 | PR | 合并后的 main |
| --- | --- | --- |
| GitHub 原始运行 | [43157805358](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43157805358) | [43159952745](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43159952745) |
| Schema/领域/HTTP | 403 项通过，另 17 个子测试通过 | 403 项通过，另 17 个子测试通过 |
| PostgreSQL 17、并发与恢复 | 835 项通过，17 项跳过，另 17 个子测试通过 | 835 项通过，17 项跳过，另 17 个子测试通过 |
| 原始 XML、GitHub ZIP 索引与选定登记回执 | [候选证据](./evidence/PRE-HARNESS/main-2768203/pr/qualification.json) | [主线证据](./evidence/PRE-HARNESS/main-2768203/main/qualification.json) |

主线登记 6 个任务、3 名演员的冻结 Skill、0 次 Attempt。工程写作夹具执行 15 次，真实模型调用 0 次，收到独立人类审读 0 份。空模板预检和容量沿用 [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) 的专项原件。历史 [Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) 仍开放，AT-30 原严格门槛 **NOT_MET**。正确性、权限、恢复、工程集成 **PASS**。真实创作保留 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`，文学质量为 `NOT_EVALUATED`。

第一阶段门禁已满足，下一阶段为 P5.7-S0。[原始十六节委托](https://github.com/SKYdayDUCK/NovelFactory/blob/05b5b0e38ffa86ed483568388aa2c2a2674f66e5/reports/integration/CREATIVE_HARNESS_DELIVERY_SCOPE.md)。
