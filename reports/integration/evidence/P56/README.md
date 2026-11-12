# P5.6 双服务综合验收回执

当前阶段正在以 `ba892d3a424be39efccb697232a5614b26609cc4` 为基线执行验收，开发分支为 `integration/p56-full-acceptance`。B 的签收条件包括本阶段的 PR 检查、合并和精确主线复验。

[基线与候选专项回执](baseline-ba892d3)包括真实 PostgreSQL 17.11 物理恢复、三个已完成的业务测试组、世界与正文 89 项专项和 9 项工件归属负控。四种预设、角色隔离与服务故障共 6 项在干净的基线工作树执行。世界与正文 89 项在同一基线上另加 `tests/integration/test_narrative.py` 的交叉回归，9 个工件归属负控为本阶段新增。组织测试 109 项通过，另有一个仅支持 Docker 的测试留给托管工作流。两端故障与恢复实测另有 4 项通过。完整性与容量分别记录，P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION。

[PR #26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) 的候选为 `e5f5348ca07dc50c9fe52ab271a733ac31fa063c`，GitHub 合成检出为 `fea6519f7d5d24b8f391648f26fee2ce794035ac`。候选的 [Workshop 三作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43141153391)已通过并完成[原始 ZIP 归属核对](candidate-e5f5348/workshop-verified.json)。

本地复验已封存 [94 个原始文件](baseline-ba892d3/local-evidence.zip)及[摘要](baseline-ba892d3/local-evidence-summary.json)，包括开发期失败、修正结果与既有容量失败边界。三档消息共 780 条，九个委托组合共 780 个唯一任务全部接受并结算。200 员工在 fixed-1、fixed-2、adaptive-2 下分别耗时 248.67、157.91、303.09 秒。数据来自本地闭环合成提供者。8 项真实旧库迁移全部通过。

[#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) 已于 02:36:05 UTC 合入 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc`，合并树与合格候选树一致。[候选逐项结果](candidate-e5f5348/candidate-qualification.json) · [各档容量原件摘录](candidate-e5f5348/capacity-measurements.json) · [合并及五个 push 运行](main-4e8fc5a/merge.json)。本时点等待实际主线复验。
