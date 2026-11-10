# 最终PR3 HEAD实际CI核验

## 身份和范围

被测提交 `23a61398278c9a13e6450d444d4a6d25a88ef5e0`，PR临时checkout `264b800bd5d019ae283b5cd7d65c08cdf742f87a`。通过GitHub connector读取两份Git commit，完整tree均为 `e5dd8cc2c2867ce6cb8d45771303b4e943d78269`；PR与push实际checkout日志也已核对。全部10份负载JSON环境中的git_sha均为该临时merge，不能把它误认为其他HEAD。

本组覆盖4个终态run、11个job、11份artifact ZIP。全部ZIP digest与GitHub公布的SHA256一致，所有重复负载、4份JUnit及11份job日志均保存。范围为PR工程/P2四格/独立200与push工程；另2个scale/PITR run单独审计，不由本索引推断。应用源码未在此次采集中更改，没有重跑workflow。

## 当前HEAD工程与业务结果

| Run | 事件/范围 | 实际结果 |
|---|---|---|
| [42864582299](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864582299) | PR工程 | PostgreSQL全套631项，0fail/0error/0skip，267.310秒；unit380项为子集 |
| [42864569773](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864569773) | push工程 | PostgreSQL全套631项，0fail/0error/0skip，377.553秒；unit380项为子集 |
| [42864580621](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864580621) | PR P2四格、每格2次 | 50正常/热点均6/6，200正常/热点均21/21；热点容量NOT_MET，workflow failure |
| [42864580618](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864580618) | PR独立200与配对 | 正常/热点均21/21，热点容量NOT_MET，workflow failure |

正常50两次、正常200三次均首试失败0。50热点首试失败133/116，最终查询均在原预算内恢复，但首试与1000 RPS门槛仍未通过。200热点三次全部正式世界提交21/21，同时存在最终未恢复的overload查询，必须分开报告。完整10次吞吐与延迟见summary.md。

| 200热点 run/repeat | 首试失败 | 最终耗尽读任务 | 少于5次/达到5次 | 有效调用/s | call/header start/s | 首试P95/P99 ms |
|---|---:|---:|---:|---:|---:|---:|
| 42864580618/single | 574 | 69 | 45/24 | 11.0787 | 44.19/30.05 | 10806.57/17297.46 |
| 42864580621/run-1 | 633 | 112 | 96/16 | 10.3340 | 32.16/22.45 | 12918.82/21761.31 |
| 42864580621/run-2 | 653 | 127 | 114/13 | 10.3539 | 32.76/22.98 | 12739.08/21083.74 |

所有耗尽项最终码BACKPRESSURE、类型overload。原政策仍为最多5次/30秒预算；少于5次说明时间预算或下一次退避无法容纳，达到5次可能同时受时间限制。completed_logical_operations并不表示所有操作成功；未删除失败请求、未将重复回执计作新世界效果。预设1000 RPS仍NOT_MET。

## 客户端与服务诊断

| 200热点run/repeat | 客户端CPU秒 | 响应验证CPU秒 | API CPU秒 | HTTPX池等待P95/P99 ms |
|---|---:|---:|---:|---:|
| 42864580618/single | 41.78 | 5.82 | 89.21 | 140.356/363.293 |
| 42864580621/run-1 | 54.72 | 8.92 | 116.47 | 111.677/532.093 |
| 42864580621/run-2 | 53.75 | 8.84 | 115.45 | 77.225/344.216 |

200热点矩阵在EPYC7763上，独立热点在EPYC9V74上，均记录4 CPU预算。独立热点1196条429全部可对应鉴权拒绝967+超时4、query拒绝96+超时129，ingress0；矩阵run1的1289条对应鉴权1026+57、query12+192、ingress2；run2的1339条对应鉴权1117+61与query11+150。因此既有客户端调度/池等待限制，也有真实服务端背压，不能只按总CPU归因。

独立热点服务inclusive CPU为query58.86、sample45.42、revision35.00、contract.validation15.26、JSON decode13.71、Context Compiler8.73秒，这些嵌套阶段不可累加。三次热点PostgreSQL pool8、峰值8、结束0、SQL错误0；最大应用事务0.594/0.366/0.460秒，cursor0.184/0.152/0.149秒。完整等待采样、响应字节、各阶段耗时与原始请求均在index/raw；诊断自身成本不能与本地关闭NF_BENCH_DIAGNOSTICS的实验混算。

## 当前源码配对与文学验收边界

| 配对实验 | PR | push |
|---|---|---|
| genesis 5对warm replay中位墙钟 | 299.86 → 27.04 ms | 339.19 → 32.30 ms |
| genesis cold单次 | 302.04 ms | 345.04 ms |
| canonical 8对中位墙钟/query | 25.095 → 24.591 ms | 34.679 → 34.760 ms |
| canonical中位CPU/query | 21.074 → 20.528 ms | 26.267 → 25.166 ms |

所有genesis完整snapshot/reference hash相等；所有canonical完整payload/evidence/cut/reference hash相等，世界before/after均未改变。push canonical墙钟略退步的实际结果也保留。配对的PASS仅SEMANTICS_AND_EVIDENCE_PASS，不代表HTTP容量或生产SLO通过。历史Context实验仍固定e0b源码及原assert，只作为历史复现；当前context-p3另有实际结果。

r1-acceptance仍保留34 PASS与3项带边界状态，外部真实模型BLOCKED_ENVIRONMENT、人工全文阅读PENDING_AUTHOR、overall_p2=NOT_FULLY_ACCEPTED。工程绿灯不能覆盖这些未验收目标或容量失败。

## 归档与合并结论

当前HEAD真实工程回归通过、约定50/200业务完成通过；热点首试/1000 RPS强制容量仍NOT_MET，不能仅凭工程绿色合并。500/1000/600秒和独立PITR必须使用同HEAD的另一组最终记录。

本组原字节证据保全到独立audit分支工作副本的evidence/pr_merge_20261111/final_heads/pr3，以避免为保存最后CI而再次改变两个PR的被测HEAD。归档成功是证据完整性结论，不是容量PASS；所有失败与较慢重复保留。
