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
