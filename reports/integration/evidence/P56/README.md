# P5.6 双服务综合验收回执

当前阶段正在以 `ba892d3a424be39efccb697232a5614b26609cc4` 为基线执行验收，开发分支为 `integration/p56-full-acceptance`。B 的签收条件包括本阶段的 PR 检查、合并和精确主线复验。

[基线与候选专项回执](baseline-ba892d3)包括真实 PostgreSQL 17.11 物理恢复、三个已完成的业务测试组、世界与正文 89 项专项和 9 项工件归属负控。四种预设、角色隔离与服务故障共 6 项在干净的基线工作树执行。世界与正文 89 项在同一基线上另加 `tests/integration/test_narrative.py` 的交叉回归，9 个工件归属负控为本阶段新增。组织测试 109 项通过，另有一个仅支持 Docker 的测试留给托管工作流。两端故障与恢复实测另有 4 项通过。完整性与容量分别记录，P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION。
