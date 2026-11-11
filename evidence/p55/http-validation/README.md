# HTTP 查询重复契约检查的同机对照

同一台 AMD EPYC 9V74 runner 按预登记的顺序运行两次原始 600 秒开放到达探针，500 个逻辑角色、20 次查询/秒、两个 API 进程和 PostgreSQL 17.11。每臂使用原基准创建独立的新数据库，夹具逻辑相同，数据库生成的标识各自独立。候选在保持公共入口请求校验的前提下减少了一次重复的 UnifiedQueryRequest 全量检查。应用 SHA 分别为 `55d8979b5361a4837701d762feacd83036096b11` 和 `df30c306057d26b7b32e3cec0df1ad9926a6824a`。[预登记](https://github.com/SKYdayDUCK/NovelFactory/blob/9bedf55314aff6e8003b22bf28734549817cb013/evidence/p55/checkpoints/query-comparison.json) · [运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43041082878/job/130592726676)

| 量度 | 原版 55d8979 | HTTP 候选 df30c30 |
|---|---:|---:|
| 请求总数 | 12,000 | 12,000 |
| HTTP 200 / 429 | 11,996 / 4 | 12,000 / 0 |
| 有效查询/秒 | 19.9456 | 19.9880 |
| 全部请求 p50 / p95 / p99（毫秒） | 130.955 / 637.346 / 1,724.740 | 123.908 / 428.202 / 650.140 |
| 服务进程用户 CPU 秒 | 747.500 | 689.615 |
| 正确性错误 / 世界提交 / 模型请求 | 0 / 0 / 0 | 0 / 0 / 0 |
| 原探针 / 原进程退出码 | NOT_MET / 1 | PASS / 0 |

本目录同时保存每臂 12,000 行的 gzip 记录、实际运行的注册文件、41 项 PostgreSQL 查询和 HTTP 回归的 JUnit 与原结果摘要。新的 HTTP 回归逐次计数；标准 HTTP 请求只校验一次，带多余字段的请求在授权前返回 422，直接调用的无效字典被拒，独立解码后改动任何副本均不改变预备请求。现有扩展服务继续收到校验后的普通字典。[逐项结果](https://github.com/SKYdayDUCK/NovelFactory/blob/1e9b45f50833c20a336669f1de35b68513932246/evidence/p55/http-validation/comparison.json) · [回归 JUnit](https://github.com/SKYdayDUCK/NovelFactory/blob/1e9b45f50833c20a336669f1de35b68513932246/evidence/p55/http-validation/candidate-junit.xml)

候选同 SHA 的独立原 500/600 秒任务也收到 12,000 次 HTTP 200，有效吞吐 19.9981 次/秒。候选 P1 两项 job 均通过，含 397 项单元测试、完整 PG17 套件 791 项通过/17 项跳过/1 条告警，以及历史 Context Compiler、完整六场景、真实 HTTP 行为和 A–H 回执。[独立 500 人运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43040949167/job/130592250673) · [候选 P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43040949065)

这对实验顺序固定、每臂在同机只跑一遍，原 long_duration_gate 返回 NOT_EXECUTED。独立原 50/200 过载、独立 200 overload 和 1000 open 仍未通过；两个候选 500 人运行不能支持对这些负载的容量声明。所有正式阈值和原始探针均保持原样。[50/200 过载](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43040949212) · [独立 200 负载](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43040949090) · [1000 open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43040949167/job/130592251132)
