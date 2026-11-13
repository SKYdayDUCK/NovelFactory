# 工程交付记录

此文件保留提交时的交付现场。后续合并及 CI 回执以关联 PR 的 GitHub 时间线和合并后的实际 SHA 为准。

从主线 `434840593179e6c1faa6b7aeca3d7d747644506d` 建立独立仓库与分支 `dev/p59-literary-harness-knowledge` 的独立 worktree。首次核查 #33 正在独立验收，#4 是 Scion 草稿。对本轮路径逐项核对最新主线后再进入正式合并阶段。

- [事先冻结量表及题材](https://github.com/SKYdayDUCK/NovelFactory/commit/a918739b4253f5ab2b8146fe1edd5d28f9ae40cf)
- [原始研究与来源](https://github.com/SKYdayDUCK/NovelFactory/commit/3b1856b934cfc86dfff767564b5f93b2259de68d)
- [实验使用的角色和技能指令](https://github.com/SKYdayDUCK/NovelFactory/commit/ecee9fc948c509996964552c3e6e5f033d0ebf1f)
- [知识库与审计](https://github.com/SKYdayDUCK/NovelFactory/commit/7f50af65d13f12de2b4bbd764cd2e9f6a2f0063d)
- [两部作品与原生草稿回执](https://github.com/SKYdayDUCK/NovelFactory/commit/502e02ade9b1e20e6a4461bcf6aab4fbc107ceff)
- [来源兼容修正](https://github.com/SKYdayDUCK/NovelFactory/commit/c8781c28e34483a6c6cfb5619fab1f8cf3b45b3d)

正文成稿、原生正式接受与 GitHub 合并分别验收。用于核查的私有 PG、凭据、原生日志、临时脚本和来源探针均在独立工作目录。提交只含 Markdown；现有 CI 和受保护的目录 JSON 没有编辑。

## 工程验收

| 实际检查 | 观察 |
|---|---|
| 原生来源与授权、Workshop 固定摘要、研究来源和链接 | [逐项独立工程复核](ENGINEERING_ACCEPTANCE.md)。最终 66/66 内建组合与原基线行为相同，三个禁止组合在两边均被拒。 |
| 架构包与文档 | `python validation/validate_package.py --output <隔离目录>/package.json`，零失败。首轮知识索引尚未落盘时，设计验证单测曾失败；补齐目标后重跑。 |
| 单元、契约与准入 | `python -m pytest -q tests/unit tests/contracts tests/service/test_admission.py`，最终 433 项和 17 个子测试通过。 |
| PostgreSQL 17.11 写作、对账、冻结与 Harness 专项 | 在独立 PG 执行 gateway_narrative、narrative、release、p58_production_pipeline、harness_literary_mcp、harness_context、harness_review_workflow、harness_editor_independence、harness_planning_simulation 和 harness_revision_brief 十个现有集成测试文件。直接覆盖受保护指令时 49 通过、3 失败，恢复内置来源字节后以完全相同的文件复跑，52 通过。 |
| 真实任务卡的投递 | 使用既有风格绑定和写作网关冻结 139 码点的 write-scene 段落，在隔离记录适配器收到的 `creative_materials` 复核字节。任务 `1454eb26-9850-41cc-8cc9-f0f297dedb7d`。适配器无外网调用，未参与小说写作或评分。 |
| 文学材料 | 13 次定稿原文送达均与原文散列一致，覆盖和盲评原件分别见两个作品目录与[第一轮报告](ROUND1_EVALUATION.md)。抽样对账保留 BLOCKED；已完成作品不冒充 NF 的 RELEASE。 |
