# P5.8-A 主线合并复验

[PR #29](https://github.com/SKYdayDUCK/NovelFactory/pull/29) 合入主线的提交为 `630c4bcf1f1a7d71f1632d6eb2d273923ac08014`。PR 头 `60641790dabe34c3b9f68a7820c794f6287372d2` 和合并提交的 Git 树一致，均为 `08d3e307fb843475dcef624897eff56bf3270bac`。原独立审查分支 `dev/p57-creative-harness-s1-s6` 保留于 `ece71f73292780211705114453ebfde1728195c5`。

| 验收范围 | 结果与原件 |
| --- | --- |
| PR 工程验证 | 六个工作流通过。包含 NF 全量、Workshop 双服务、P5.7 Creative Harness、P4 物理 WAL 恢复、原生桌面恢复及 P6 离线授权。托管 NF 全量记录为 973 passed、18 skipped、17 个 subtests passed。各作业、失败步骤及原始日志链接见 [GitHub 原始索引](raw/merged-main/github-pr.json)。 |
| 已合并 main 单独运行 | 从 GitHub 获取合并提交，在干净工作树上对空 PostgreSQL 17.11 和 Redis 运行 [P6 专项](raw/merged-main/p6-on-merged-main.xml)。再按手册建立两本小说，分别恢复两套独立 PG 物理集群，详见[双服务与错位恢复](raw/merged-main/operator-restore-on-merged-main.xml)。 |
| 独立操作者验收 | 第三次从空库启动，复核版本、授权、双角色、预算、无密钥预检和原生 CLI 与本地协议。零预算预演与正式预算建书的目录行为已实测。[独立记录](independent-operator/INDEPENDENT_RECHECK.md) |
| 历史容量 | 三个历史测量工作流仍有严格场景未达标。50/200 Agent 混合、500 混合和 500 读长测通过。50/200 过载及 1000 开放读仍有 NOT_MET。指数、阈值与原始退出码保留在原作业，Issue #13 / AT-30 继续开放。[Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) |
| P6 真实创作 | 无付费授权。新功能验收的线上推理请求为 0，双供应商实调为 NOT_EXECUTED，真实已结算金额为 null，独立读者评分 0 份，文学质量 NOT_EVALUATED。Windows Docker Desktop 的实际启动待现场验证。 |

阶段状态为 **MERGED_AND_VERIFIED**。本次 P6 低并发工程范围、逐项验收和复现命令分别见[验收矩阵](ACCEPTANCE.md)与[操作者手册](README.md)。
