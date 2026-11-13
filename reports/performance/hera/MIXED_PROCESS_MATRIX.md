# Split-process mixed REST and SDK status reads

The application (`src` Git tree `03878254974cdf327477a631eff133528bce7c80`, matrix driver blob `f561cd1a6f8844ebb00f3384b6ee051b51f07f0c`) used PostgreSQL 17.11, a four-connection server pool (zero overflow), two business slots and two authentication slots. All arms enabled fenced task reads and `NF_HERA_FUSE_STATUS_AUTH`. PostgreSQL and the client process shared CPU 6 for the first 28 cells and the distinct-client burst. A host SIGKILL interrupted the first client process during the 5000/80% cell, prior to writing a result. Its server then lost the control pipe and the launcher shut down its test database. The last two 5000-agent cells used a recreated database on CPU 4, client on CPU 6, server on CPU 7. A separate preload process gate delayed the restart until the latter CPUs were released. No signal source was inferred. [Interrupted client transcript](raw/fused-mixed-processes/first-matrix.log.gz) and [PG log](raw/fused-mixed-processes/first-matrix-pg.log.gz). The single separate Uvicorn process used CPU 7. The server routes both protocols to the same PostgreSQL-backed task API. These tests measure read-only access to pre-existing durable READY tasks, with zero execution workers and zero model slots. Each response was checked against its exact task ID, full WorldRef, state and generation.

## Matrix with 20 live authenticated clients

Each row addresses distinct durable tasks (a different task per logical agent) on two branches in different works. The preload is 5,000 tasks throughout. A logical agent here is one task read issued on a client lane. The 20 authenticated principals each own a disjoint set of these tasks. SDK clients initialize and wait for the common release; REST clients first perform an authenticated read. The worker-free status test has no new admission, queue recovery or model completions. Each cell is one run at that population/ratio. The run order was ascending population then MCP percentage. Confidence intervals and causal throughput gains require paired arms.

| Logical agents | HTTP 100% | HTTP 80%, MCP 20% | HTTP 50%, MCP 50% | HTTP 20%, MCP 80% | MCP 100% |
|---:|---:|---:|---:|---:|---:|
| 50 | 129.3 / 235 / 243 | 71.5 / 487 / 500 | 30.1 / 1431 / 1450 | 64.0 / 503 / 512 | 46.8 / 666 / 688 |
| 200 | 168.0 / 192 / 211 | 109.5 / 382 / 439 | 87.9 / 328 / 423 | 46.3 / 730 / 882 | 56.1 / 541 / 597 |
| 500 | 122.9 / 319 / 340 | 96.8 / 323 / 366 | 99.5 / 300 / 346 | 57.0 / 642 / 718 | 57.0 / 591 / 712 |
| 1000 | 221.4 / 109 / 134 | 131.9 / 236 / 636 | 97.7 / 293 / 334 | 63.4 / 648 / 787 | 64.9 / 380 / 784 |
| 2000 | 171.7 / 153 / 204 | 117.3 / 263 / 353 | 55.5 / 743 / 1071 | 51.4 / 641 / 1020 | 40.8 / 1056 / 1399 |
| 5000 | 73.8 / 648 / 1048 | 50.1 / 768 / 1251 | 46.4 / 735 / 1288 | 50.2 / 625 / 899 | 46.7 / 701 / 970 |

Cells give **correct reads/s / P95 ms / P99 ms**. All 43,750 measured reads passed, with 20–20 client operations in flight at peak. HTTP-only, 20%, 50%, 80% and 100% MCP used 0, 4, 10, 16 and 20 live SDK sessions respectively. Each row of each [compressed cell](raw/fused-mixed-processes) includes the logical agent, authenticated client lane, actual transport, task ID, send/response timestamps, outcome and verification. Each capture records HEAD at the end of its cell. Evidence/other-driver commits advanced that reference during the run. All observed revisions share the product tree and the mixed driver blob above. Peak RSS, server queues and client CPU are included per cell. The CPU deltas include client initialization and teardown. The response quantiles cover actual calls from individual sends. Matrix queue high-water counters are process-lifetime snapshots before and after each cell.

## Independent one-thousand-client simultaneous release

A separate fresh database held exactly 1,000 READY tasks, each owned by a distinct authenticated principal. 500 initialized SDK sessions and 500 REST clients all passed the release barrier. Peak in-flight reads reached 1,000. All 1,000 matched their own frozen fixture. A pool of four and the bounded queues drained the burst in 23.18 seconds at 43.14 correct reads/s. P50/P95/P99 were 4.35/22.40/22.70 seconds. The ingress queue reached 436 waiters; business admission reached 62 waiters. [Every request and queue snapshot](raw/fused-mixed-processes/n1000-c1000-p50.json.gz).

The 100 ms P95 target is assessed per cell. The burst and 100% MCP matrix cells exceed it. The queue snapshots show waiters at both authentication and business admission. This matrix did not vary either slot count. Protocol costs have a separate [controlled ablation](MCP_STATUS_AUTH.md).

Reproduce both workloads against an isolated PostgreSQL 17 test cluster:

```sh
taskset -c 6 python -m benchmarks.hera_mixed_processes --server-cpus 7 --clients 20 --output /tmp/hera-matrix
taskset -c 6 python -m benchmarks.hera_mixed_processes --server-cpus 7 --clients 1000 --sizes 1000 --percent 50 --output /tmp/hera-burst
```
