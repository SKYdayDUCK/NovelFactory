# NF Bridge 候选容量检查（PR #11）

候选为 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c`，Actions 实际检出 `3cdb5fbb74b06386099d9a2829ac83fc37bb8ab0`，两者树均为 `28dcb1d0ef5ce4e05241318aafd9c52fb2824267`。第一父为 W2 主线 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。[源码与检出](candidate.json)

| 工作流 | 完成结果 | 原始 ZIP 数 |
|---|---|---:|
| [P3 P4 measured evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135154) | failure | 3 |
| [P2 measured capacity and recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135306) | failure | 4 |
| [P3 scale experiments with strict unmet gates](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135239) | failure | 3 |

| 测试 | 重复 | 原门槛 | 实际业务结果 | 首次/调度开始 (req/s) | 请求头开始 (req/s) |
|---|---|---|---|---:|---:|
| p2-load-200-mixed | run-1 | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 1141/1141，未解决业务错误 0 | — | — |
| p2-load-200-mixed | run-2 | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 1141/1141，未解决业务错误 0 | — | — |
| p2-load-200-overload | run-1 | NOT_MET | 正确性 PASS，有界恢复 PASS，逻辑操作 2141/2141，未解决业务错误 0 | 36.96 | 31.03 |
| p2-load-200-overload | run-2 | NOT_MET | 正确性 PASS，有界恢复 PASS，逻辑操作 2141/2141，未解决业务错误 0 | 36.69 | 30.33 |
| p2-load-50-mixed | run-1 | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 286/286，未解决业务错误 0 | — | — |
| p2-load-50-mixed | run-2 | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 286/286，未解决业务错误 0 | — | — |
| p2-load-50-overload | run-1 | NOT_MET | 正确性 PASS，有界恢复 PASS，逻辑操作 536/536，未解决业务错误 0 | 78.64 | 52.28 |
| p2-load-50-overload | run-2 | NOT_MET | 正确性 PASS，有界恢复 PASS，逻辑操作 536/536，未解决业务错误 0 | 87.59 | 53.24 |
| p3-context-paired | — | 完成 | 15 组同输入比较，世界状态一致=True | — | — |
| p3-isolated-200-mixed | — | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 1141/1141，未解决业务错误 0 | — | — |
| p3-isolated-200-overload | — | NOT_MET | 正确性 PASS，有界恢复 PASS，逻辑操作 2141/2141，未解决业务错误 0 | 37.52 | 31.35 |
| p3-scale-1000-read-open | — | NOT_MET | 200=1856，429=3126，transport_error=18，正确性错误 0，新增世界提交 0 | 42.17 | 40.96 |
| p3-scale-500-mixed | — | PASS | 正确性 PASS，有界恢复 PASS，逻辑操作 2851/2851，未解决业务错误 0 | — | — |
| p3-scale-500-read-soak | — | PASS | 200=12000，正确性错误 0，新增世界提交 0 | 20.00 | — |

50/200 overload、独立进程 200 overload 和 1000 开环读的原门槛在先前候选也未达到。950 次/秒来自开放到达目标 1,000 的 95%。在同机 A–B–B–A 比较中，前置 SHA 和旧桥接 SHA 均低于该线。500 人 600 秒持续读单独统计；长期稳定性门槛为 `NOT_EXECUTED`。[Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) · [历史配对结果](https://github.com/SKYdayDUCK/NovelFactory/issues/13#issuecomment-6799018612)

旧桥接候选 `90cac947` 的 1,000 开环读在 5,000 次请求中有 2,045 个 HTTP 200、2,947 个 429、8 个传输错误，实际请求头开始速率 47.80 次/秒。当前轮对应 1,856、3,126、18，40.96 次/秒。各轮来自不同托管主机，该差异不能单独归因于桥接实现。[原始历史作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43047258923/job/130614413492)

[每项指标及原始 JSON 的哈希](verified-metrics.json) · [作业、原始工件 ID、下载入口、大小与到期日](workflow-index.json) · [下载后验证的 ZIP SHA-256](SHA256SUMS)。`archives/` 收录本次三个工作流全部十个原始 ZIP。
