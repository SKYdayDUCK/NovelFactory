# W6 首次主线集成候选的正式回执

PR #15 的源提交为 `23d195d9dcd901506f6571d902e68fd48e5a0ab7`。托管预合并提交 `4280c00134e0627521a936e7892950ebe2b18748` 使用相同工程树 `1ef8c2b9568f8ca161d80fc9be2286ed4c18d8a2`。

六条工作流及全部 17 个工件的 GitHub 标识、SHA-256、原始大小与逐步检查记录见 [索引](workflow-index.json) 和 [归档清单](artifacts.json)。四类历史容量门槛按原值未达。NF 单元/契约 398 通过，独立原生 PostgreSQL 验收 834/834，原生双服务、双 SSH、物理 PITR 与八个旧库升级均有正式回执。三个归档分组分别保留 Workshop、NF 工程与历史容量数据，独立 NF 回执单列。

首次 Workshop 工作流报告两项真实失败。比较组 Long-form 会在 PostgreSQL 允许下次查询之前取得 IDLE，原始容量组因工作目录缺失根模块路径而无法收集委托测试。两项错误均已在独立复演中验证。一个托管失败 XML 捕捉了临时数据库连接凭据，归档删除其中的 userinfo，原始工件和 XML 的哈希均在清单内。候选本身未合并。替代候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`。

[托管 Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43116965757) · [原始性能跟踪](https://github.com/SKYdayDUCK/NovelFactory/issues/13) · [结构化测量](measurements.json) · [独立 NF JUnit](independent-nf.zip)。
