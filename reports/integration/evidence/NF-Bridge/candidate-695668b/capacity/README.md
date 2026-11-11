# NF Bridge 候选容量复验（PR #11）

冻结候选为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。GitHub 实际检出 `dff58688fdb1f931a5906f885ed513ed12134026`，合成提交第一父为 W2 主线 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。合成提交与候选同树 `788dc4b190299894509593a23982452f0362f9ef`。[检出记录](candidate.json)

| 工作流 | 终态 | 原始归档数 |
|---|---|---:|
| [P3 P4 measured evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588179) | failure | 3 |
| [P2 measured capacity and recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588236) | failure | 4 |
| [P3 scale experiments with strict unmet gates](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588056) | failure | 3 |

此前候选为 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c`。下表“开始”指开放到达负载的首次调用/调度开始速率，两个读测试同时保留响应分布和实际传输数据。托管作业使用独立主机，本表提供原量纲观察值。[此前全部作业及原件](https://github.com/SKYdayDUCK/NovelFactory/tree/ba8ce3059b7756eaea30ab8a064f2ea77d977157/reports/integration/evidence/NF-Bridge/candidate-26bf033/capacity)

| 原负载（重复轮次） | 此前门槛 | 本次门槛 | 本次业务结果 | 此前→本次开始 req/s |
|---|---|---|---|---|
| p2-load-200-mixed (run-1) | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 1141/1141，世界修订 21/21 | — → — |
| p2-load-200-mixed (run-2) | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 1141/1141，世界修订 21/21 | — → — |
| p2-load-200-overload (run-1) | NOT_MET | NOT_MET | 正确性 PASS，恢复 PASS，逻辑完成 2141/2141，世界修订 21/21 | 36.96 → 36.19 |
| p2-load-200-overload (run-2) | NOT_MET | NOT_MET | 正确性 PASS，恢复 PASS，逻辑完成 2141/2141，世界修订 21/21 | 36.69 → 34.85 |
| p2-load-50-mixed (run-1) | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 286/286，世界修订 6/6 | — → — |
| p2-load-50-mixed (run-2) | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 286/286，世界修订 6/6 | — → — |
| p2-load-50-overload (run-1) | NOT_MET | NOT_MET | 正确性 PASS，恢复 PASS，逻辑完成 536/536，世界修订 6/6 | 78.64 → 165.86 |
| p2-load-50-overload (run-2) | NOT_MET | NOT_MET | 正确性 PASS，恢复 PASS，逻辑完成 536/536，世界修订 6/6 | 87.59 → 227.50 |
| p3-context-paired | 已完成 | 已完成 | 15 组对比，世界状态保持一致 | — → — |
| p3-isolated-200-mixed | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 1141/1141，世界修订 21/21 | — → — |
| p3-isolated-200-overload | NOT_MET | NOT_MET | 正确性 PASS，恢复 PASS，逻辑完成 2141/2141，世界修订 21/21 | 37.52 → 35.00 |
| p3-scale-1000-read-open | NOT_MET | NOT_MET | 200=1745，429=3242，transport_error=13，正确性错误 0，新增世界提交 0 | 42.17 → 41.86 |
| p3-scale-500-mixed | PASS | PASS | 正确性 PASS，恢复 PASS，逻辑完成 2851/2851，世界修订 51/51 | — → — |
| p3-scale-500-read-soak | PASS | PASS | 200=12000，正确性错误 0，新增世界提交 0 | 20.00 → 20.00 |

1,000 人开环读实际请求头开始速率由 40.96 变为 40.73 次/秒，有效查询吞吐由 15.13 变为 14.12 次/秒。500 人 600 秒持续读记录为 {'200': 12000}，长期稳定性门槛为 `NOT_EXECUTED`。

表中未达标项都对应 Issue #13 已跟踪的原容量场景。两候选每个有写入的负载中，实际世界修订数均等于预期，资源检查与连接释放通过、有界恢复通过；两个只读负载的世界状态均保持一致。

所有负载记录的模型请求数为 0。有写入的服务负载将外部模型状态记为 `NOT_DISPATCHED`、Token 数为 0、账单字段为空。后置结算、UNKNOWN 和晚到回执需要桥接专项验收，本容量数据没有生成这类账单。[独立的桥接账本与物理恢复记录](../nonempty-physical-pitr.json)。

[每项指标](verified-metrics.json) · [与前一候选的逐项对照及差异](compare-26bf0337.json) · [官方作业和工件索引](workflow-index.json) · [原 ZIP 校验清单](SHA256SUMS) · [历史容量 Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)。三个工作流的十个原始 ZIP 均已逐字节归档。
