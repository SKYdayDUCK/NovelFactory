# Concurrent REST and SDK Streamable HTTP

The normal application ran on one loopback TCP port. Both adapters shared one PostgreSQL 17 engine, capped at four connections (zero overflow), with two authentication and two business slots. Each client used its own bearer identity. The experiment enabled durable admission. This workload configured finite server queues and did not retry unsuccessful commands. The load generator and the ASGI server used separate loops in one process. A 300 s keepalive preserved warmed clients until the common release. No model ran in the following TaskSpec-only series.

| Submitted commands | Live principals | Initialized MCP sessions | REST clients | Verified READY in PostgreSQL | Acknowledgements/s | Last response, s | P95 per request, ms |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 200 | 20 | 10 | 10 | 200 | 26.07 | 7.67 | 867 |
| 500 | 20 | 10 | 10 | 500 | 19.28 | 25.93 | 1,424 |
| 1,000 (simultaneous) | 1,000 | 500 | 500 | 1,000 | 15.60 | 64.11 | 60,925 |
| 2,000 | 20 | 10 | 10 | 2,000 | 19.40 | 103.10 | 1,559 |
| 5,000 | 20 | 10 | 10 | 5,000 | 25.28 | 197.75 | 975 |

The 1,000-client burst reached 1,000 client operations in flight, four checked-out database connections, 912 pending ingress requests, 48 queued for authentication and 62 queued for execution. These are distinct peaks and cannot be added. All 1,000 acknowledgements were durable and there were zero 429s. Peak RSS of the combined client and service process was 879,100 KiB. The population series used bounded concurrency; each TaskSpec has its own durable task ID. The 5,000-case process sampled peak RSS of 121,988 KiB after previous arms, not 5,000 live processes. TCP connection counts, model calls and durable task counts have different denominators. See the dedicated depth study for relation sizes, memory and write costs. [Burst](raw/burst/) · [Population results](raw/population/) · [Queue growth](ADMISSION_DEPTH.md)

An earlier 1,000-client driver attempt aborted when independent SDK streams raised HTTP read errors. That attempt did not produce a verified completion count. We preserved the [original failure](raw/first-burst-driver-error.log.gz), separated lane exceptions and explicitly configured keepalive for the warmed burst. The dataset above was collected after that change.

Run `python -m benchmarks.hera_mixed --clients 1000 --populations 1000 --mcp-pct 50 --order B --operation submit --output /tmp/hera-burst` against an isolated PostgreSQL administration URL. The SDK version, workload and SQL counters are embedded in each burst/population record. [Cross-protocol lifecycle regression](../../../tests/service/test_hera_mixed_ingress.py) · [Lifecycle with automatic model execution](../../../benchmarks/hera_delegated_burst.py)

## Supervised model work after disconnect

A second, separate 1,000-client burst used 1,000 independently provisioned installation credentials, two books, 500 actual remote SDK sessions and 500 REST clients. Both paths used the same delegation transaction. Each task was authorized for a specific simulated summary route, monetary/token limit and frozen inputs prior to the burst. All 1,000 acknowledgements referenced the correct operation ID and task ID. After closing all clients and the ingress server, the driver disposed the database engine, created a new kernel/supervisor against the same PG and drained the jobs.

| Check | Result |
|---|---:|
| Last acknowledgement after simultaneous release | 60.01 s |
| 95th percentile of acknowledgement time | 57.51 s |
| Recovered durable delegations / accepted terminal results | 1,000 / 1,000 |
| Maximum simultaneous model calls | 2 |
| Simulated model calls / successful billed-attempt ledger records | 1,000 / 1,000 |
| Incorrect generation, missing current result or duplicate model attempt | 0 |
| Remaining reserved amount across two work budgets | 0 |
| Drain starting at supervisor restart, through verified delivery | 534.06 s |

The simulated provider returned known, zero-cost usage. This scenario exercised actual task claim, fixed context, attempt accounting, result storage and acceptance. The older 35.26 s role-read burst has no model execution and uses a different timing interval. [All 1,000 responses and corresponding final PostgreSQL rows](raw/supervised-1000-final.json.gz) · [Driver](../../../benchmarks/hera_delegated_burst.py)

## Same role-read contract as the historical 1,000-client burst

A real `nf_task_get` response had to equal its complete role-specific reference on every client. One Uvicorn process and the load driver shared a Python process, using a physical pool of two PostgreSQL connections, zero overflow, two works, 1,000 unique grants and 1,000 initialized remote SDK sessions. The timer opens after all initializations. The calls then start together with no client-side two-slot limiter. Every arm returned 1,000 correct responses, zero 429s, zero model attempts and unchanged world heads. A preliminary split-pool run drained in 51.29 s and returned 554 responses within 30 seconds. [Preliminary raw responses](raw/actor-1000.json.gz)

| Mode in execution order | Last complete response (s) | Completed in 30 s | P50 / P95 / P99 (s) |
|---|---:|---:|---|
| A1, one business lane + one authentication lane | 71.16 | 376 | 37.22 / 67.60 / 70.54 |
| B1, two jointly bounded lanes | 48.19 | 572 | 26.49 / 45.91 / 47.85 |
| B2, two jointly bounded lanes | 52.75 | 587 | 24.87 / 50.37 / 52.32 |
| A2, one business lane + one authentication lane | 68.58 | 393 | 36.76 / 65.53 / 68.01 |

Sharing the two slots reduced total drain time by 32.3% and 23.1% against the adjacent split arms. The historical Host queue finished the same operation in 35.26 s with 860 correct responses in 30 s; neither candidate arm met that target here. Shared CPU load, client placement and software versions across historical and contemporary measurements can differ. On the current server a successful `nf_task_get` includes bound-tool verification and audit. Keep these measurements distinct from fast task metadata and from asynchronous model submission. The pool allocation remains optional through `NF_HERA_SHARE_AUTH_SLOTS`. No fleet-level Host limiter was applied to the four arms. To replay from frozen `4f809f3`, run `python -m benchmarks.hera_actor_burst --clients 1000 --order ABBA --output /tmp/actor.json.gz` against a fresh disposable PG17 cluster. [Preregistered comparison](ACTOR_BURST_PLAN.md) · [A1](raw/actor-1000-0A.json.gz) · [B1](raw/actor-1000-1B.json.gz) · [B2](raw/actor-1000-2B.json.gz) · [A2](raw/actor-1000-3A.json.gz)

## Admission of 5,000 distinct authorized writers

One fresh task per principal, grant and credential was offered at the common release. Each work received 1,250 REST and 1,250 remote MCP submissions. The driver admitted up to 20 identity lifecycles at once. Each MCP identity opened a fresh Python SDK session. The application served real loopback TCP and held a four-connection PostgreSQL pool with zero overflow. Grant provisioning was outside the measured interval. Both the 20-identity smoke and the 5,000-identity result passed the registered checks.

| Submit transport | Offered | Correct READY acknowledgements | Initialized SDK sessions | Call P50 / P95 / P99, ms | Last acknowledgement after release, s |
|---|---:|---:|---:|---:|---:|
| Remote MCP | 2,500 | 2,500 | 2,500 | 1,680 / 2,494 / 3,231 | 376.40 |
| REST | 2,500 | 2,500 | 0 | 649 / 1,059 / 1,525 | 375.70 |

The last positive reply arrived at 376.40 s and the last client closed at 376.40 s. Whole-population goodput including handshakes was **13.28 correct acknowledgements/s**. Release-to-response P50/P95/P99 was 199.18/359.00/372.89 s. Those timings include waiting for a client lane. Creating the two fixture works, issuing all grants and preparing commands took 21.28 s before release. The earlier 20-principal run reused warmed clients for 5,000 commands in 197.75 s (25.28/s). These workloads have different connection lifecycles and do not supply a paired speedup.

| Resource during positive admissions | Observed |
|---|---:|
| Peak live identity lifecycles / initialized SDK sessions / business calls | 20 / 18 / 20 |
| Peak established API TCP connections, sampled every 0.5 s | 20 |
| Peak PG pool checkouts / checkouts after drain | 4 / 0 |
| Ingress / authentication / operation peak active | 20 / 2 / 2 |
| Ingress / authentication / operation peak queued | 0 / 18 / 18 |
| Combined load-generator and API CPU seconds / process lifetime peak RSS | 418.76 s / 138,512 KiB |
| Four PG client backends' CPU seconds / peak sum of sampled RSS | 55.13 s / 193,392 KiB |
| Application SQL statements / failed statements | 152,515 / 0 |

After shutdown fresh physical DB connections verified 5,000 READY tasks, 5,000 durable admission rows, 5,000 matching creation commands, 5,000 distinct stored credential hashes and one task-change outbox record for every expected task. Both branch heads were unchanged, with zero model attempts. All positive requests succeeded and all three admission queues drained. Separately, each protocol rejected a cross-work read and an over-capacity submit. REST returned 403 and 429. Remote MCP returned VIEW_DENIED and retryable BACKPRESSURE with retry_after_ms=1000. The controls left the verified counts unchanged.

PG CPU and sampled RSS cover the four application backends. Summed RSS includes shared pages once per backend. The single low-frequency TCP/RSS sampler can miss instantaneous peaks. Client and server CPU share one Python process. The completion boundary is committed READY with no model execution. Reproduce on frozen source `ca2c4c7` with `python -m benchmarks.hera_distinct_admission --count 5000 --output out.json.gz` using an isolated PG17 administrator URL in `NF_TEST_DATABASE_URL`. [Preregistration](IDENTITY_ADMISSION_PLAN.md) · [Per-identity receipts and telemetry](raw/distinct-admission-full.json.gz) · [Smoke run](raw/distinct-admission-smoke.json.gz)

## 2026-11-13 分进程复验

在两个作品上预置 5,000 项不同的 READY 任务。对 50、200、500、1,000、2,000、5,000 六种逻辑规模分别运行 0%、20%、50%、80%、100% MCP，30 格共 43,750/43,750 项状态与权威任务匹配。常规矩阵每格使用 20 个独立授权身份，SDK 会话数依次为 0、4、10、16、20；其余客户端发 REST。在途数、实际建立的 MCP 会话、每个被查任务、持久任务总数和模型槽均在各格原始记录中分列。服务池四连接，读取阶段模型槽使用数为零。最后两格复跑时数据库 CPU 亲和性发生变化，具体条件见矩阵报告。

单独同时放行 1,000 个独立授权客户端（500 MCP、500 REST），峰值 1,000 个在途请求，1,000/1,000 项正确，23.18 s 排空，P95 为 22.40 s。5,000 逻辑任务、20 活动客户端的 100% MCP 格为 46.72 次/秒，P95 为 701 ms，P99 为 970 ms。此批全矩阵使用冻结的 MCP 优化代码。SDK 单项授权融合、深队列精确计数和最终组合分别测量。

跨协议操作复跑 182 项，含 154 次授权成功和 28 次预期拒绝，创建、重放、取消、任务所有权、WorldRef、代际及状态均按预期核对。另启动 40 个独立委托客户端（20 个 MCP 会话，20 个 REST 客户端），响应、任务、世代及 Provider 账本逐项对账；销毁入口服务、重新建立连接池后 40/40 得到 ACCEPTED，40 次模拟 Provider 调用、两部作品剩余预留均为零。服务重启后的排空时间为 12.59 s。早期 1,000 项委托数据保留在原报告中。

[全矩阵及全部逐请求记录](MIXED_PROCESS_MATRIX.md) · [混合授权、拒绝与幂等](raw/component-reruns/mixed-operations/) · [重启、交付和逐项账本](raw/component-reruns/delegations-rerun.json.gz)
