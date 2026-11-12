# S9 历史证据 PR #18 归档与处置

原 [PR #18](https://github.com/SKYdayDUCK/NovelFactory/pull/18) 在已冻结的 P5.5 源码 `55d8979b5361a4837701d762feacd83036096b11` 上开展可重复的工程比较。原头 `3985ebd4f90ff9f7d2e9626df9c0fbf5e8cdb1be` 含 9 个提交，相对该基线新增 12 个文件，未删除或修改原基线文件。7 个历史工作流、2 个取证脚本和 3 份登记文件的原始 Git blob 均按原字节保存在 [source-root](source-root)。运行时代码、世界状态、数据库迁移或调度代码均未包含在这 12 个变更中。[逐文件对照](source-inventory.json)。

| 保存与采用 | 证据和决策 |
| --- | --- |
| S9 语义保护比较 | 原始三个固定标签和同一组 PostgreSQL 输入预测 A/B/C/D。误判/漏判分别为 1/0、0/1、0/1、0/0。采用这些有限样本的工程观察，外推至其他作品和文学质量均未评估。被测的正式实现随 [#6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) 进入主线。 |
| SQL 与 HTTP 查询 | [#20](https://github.com/SKYdayDUCK/NovelFactory/pull/20) 的 SQL 构造缓存保留为历史实验，独立原工作流仍有 102 个 429。正式采用的 HTTP 查询校验改动见 [#21](https://github.com/SKYdayDUCK/NovelFactory/pull/21)，原 600 秒对照见 [#22](https://github.com/SKYdayDUCK/NovelFactory/pull/22)。结果和源码分别记录。 |
| 大世界、准入和桌面 | 同机探针和大世界读的原始输入、时序、门槛及结果随原 ZIP 保存。正式的投影完整性修复由 [#24](https://github.com/SKYdayDUCK/NovelFactory/pull/24) 和 #6 接收。历史桌面作业只触发和引用当时实际主线已有的测试。 |
| 工程写作对照与容量 | [#23](https://github.com/SKYdayDUCK/NovelFactory/pull/23) 的真实 NF writer 工程运行、确定性模型和返工对照与 #18 的保护决策是不同实验。前者随 #16 存入主线 `evidence/p55/business`。容量原门槛仍由 [#13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) 跟踪。 |

[原 ZIP 索引](original-archives.json)列出 10 个原始实验包、全部 80 个成员、2,025,233 字节、原始 run、artifact ID、官方 SHA-256、归档 Git blob 和成员摘要。剩余三个专用作业没有工件，其中包括向证据分支发布结果和调度既有桌面工作流。存放位置保留每个历史工作流文件的原名，同时使其无法作为现行 CI 触发。归档前，工具用只读权限从 GitHub 下载并验证每个 ZIP，在分块传输后再次校验整包。[实际取证工作流](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43155669120)。

处置决议为将原 PR 作为历史实验关闭。其运行源码、输入、完整实验输出与采用决策均可查回。当前仓库内的正式工程交付以 [P5.6](https://github.com/SKYdayDUCK/NovelFactory/blob/3ef5a72d7a65edb76a988132a72c4e82ee0805ee/reports/integration/evidence/P56/main-4e8fc5a/SIGNOFF.md)和 [P6](https://github.com/SKYdayDUCK/NovelFactory/blob/a9d77b86c425de3cfe67c1b82996377003ac4200/reports/integration/evidence/P6-C2/main-0420356/SIGNOFF.md) 的主线签收为准。

[间接输入与下游回执](related-archives.json)另存 3 份原始 600 秒负载 ZIP 和 1 份原生桌面 ZIP，共 1,469,088 字节。三份上游 result.json 的原字节摘要均匹配已发布的历史摘要，各含 12,000 行原数据；没有真实模型调用。所有分支作业位于[工作流、作业和工件索引](workflows-jobs-artifacts.json)，原 #13 条件见[容量边界](issue13-capacity-boundary.json)。

[#20 的原实现及测试](subjects/598f4ee)单独保存，以便重现相关 600 秒对照。正式主线保留既有 repository 查询路径，未采用此结构缓存。[逐文件与正式回归审计](code-and-decisions.json)。
