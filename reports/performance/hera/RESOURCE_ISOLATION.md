# Control and status work during executor backlog

Real SDK over loopback Streamable HTTP and REST share one four-connection PostgreSQL 17.11 pool with zero overflow. Ten live identities send 100 status requests per arm, five sessions on each transport, across two works. The same fixed 2-model-slot global and two-index-slot maximum applies. Status checks verify the owner, generation, state and WorldRef. Wall time and P95/P99 service latency include contention from the shared client/server Python process. Quiet/loaded/loaded/quiet ordering; each pair compares the left/right outer quiet arm to its adjacent loaded arm. All 800 responses were correct across these two experiments.

| Experiment and arm order | Verified requests | Goodput in requests/s | P95 in ms | P99 in ms |
|---|---:|---|---|---|
| Quiet / model / model / quiet | 400/400 | 46.46 / 48.46 / 49.74 / 53.33 | 280.87 / 275.64 / 288.63 / 247.63 | 316.31 / 354.25 / 323.61 / 259.23 |
| Quiet / index / index / quiet | 400/400 | 52.00 / 28.38 / 25.40 / 39.88 | 258.54 / 419.01 / 497.34 / 444.41 | 265.57 / 438.31 / 580.12 / 498.58 |

During each model arm the live provider ledger had two SENT calls and two later READY tasks. The fixture's generation barrier held both in-flight jobs throughout the reads. The shared engine had zero checked-out connections after provider entry, and all eight jobs across the two loaded runs ended RESULT_STORED with one attempt each. Throughput deltas were +4.31% and −6.74%, while P99 degraded 11.99% and 24.84%. The worst reciprocal tail missed the 20% gate.

Each database-heavy arm seeds 100 real world transfers and builds their immutable indexes on two actual workers. 33 and 34 index completions overlapped the measured read intervals. Both drains produced 100 distinct source receipts and verified the authority's final index watermark. Fast throughput fell 45.43% and 36.30%; P99 increased 65.04% and 16.35%. Heavy workers and readers still contend for Python CPU and the database; this experiment cannot partition the loss between them.

Replay `python -m benchmarks.hera_model_isolation --output /tmp/model.json.gz` and `python -m benchmarks.hera_db_isolation --output /tmp/db.json.gz` with `NF_TEST_DATABASE_URL` pointing to isolated PostgreSQL 17. Model source and environment are in its raw JSON. DB source was 36c4403 plus the benchmark changes landed in 15dc604. Tests on 65e5afe and 443ea20 exercise class caps, saturated control recovery, compute outbox ACK, expired leases and immutable index effects. Rolling back `--control-while-busy` and removing the configured RESOURCE rows restores the previous scheduler policy. [Model reads and task outcomes](raw/model_isolation.json.gz) · [Index reads and per-event completions](raw/db_isolation.json.gz) · [Preregistration](RESOURCE_EXPERIMENT_PLAN.md)

## Two-work, 2,000-identity workflow

The 65e5afe candidate ran `python -m benchmarks.p59_workflow_load --agents 2000 --output /tmp/workflow.json` on two books and 2,000 independently granted principals with a shared eight-connection pool and two model slots. It registered and metered real PostgreSQL tasks, call attempts, wait DAGs and results. Both books reached all expected dispositions. Exactly 399 barriers woke, 1,958 tasks stored results, two prerequisites were accepted, and twenty cancelled before send. Twenty injected provider-response losses paused with UNKNOWN. No incorrect task, leftover slot or changed world head was observed. The 1,980 in-process provider calls peaked at two concurrent. PostgreSQL peak checkout was four; client/service peak RSS was 88,464 KiB. Provisioning and drain took 914.74 s together, including 787.33 s for drain (2.489 correct successful results/s). The measured two-book task outcomes reflect fault injection by input index, not a comparative fairness gain. This run sends zero ingress requests and does not measure a 2,000-connection API. [Every task and ledger outcome](raw/workflow_2000.json.gz)

## Follow-up with one or two concurrent index leases

Two worker threads competed under the shared four-connection pool in every run. Each arm offered 100 status reads and 100 durable world-index builds. The one-client tier used REST; the ten-client tier used five MCP SDK sessions and five REST clients. Index workers waited on capacity and resumed until every source event had exactly one index receipt. The index records and caller timings share one monotonic clock. The quiet reference ordering above is a different experiment.

| Clients | Index slots | Reads/s | Read P95 ms | Read P99 ms | Indexes/s during reads | Indexes/s through drain | Indexes finished during reads |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 2 | 11.23 | 140.50 | 240.87 | 11.23 | 13.06 | 100 |
| 1 | 1 | 13.11 | 99.84 | 127.44 | 11.41 | 11.73 | 87 |
| 1 | 1 | 11.34 | 152.67 | 307.59 | 10.21 | 10.23 | 90 |
| 1 | 2 | 10.71 | 130.52 | 151.41 | 10.71 | 12.04 | 100 |
| 10 | 2 | 27.07 | 553.28 | 630.88 | 8.93 | 11.80 | 33 |
| 10 | 1 | 30.47 | 468.98 | 515.94 | 5.79 | 9.85 | 19 |
| 10 | 1 | 26.22 | 594.25 | 653.74 | 4.72 | 7.59 | 18 |
| 10 | 2 | 25.27 | 490.91 | 576.60 | 9.35 | 12.18 | 37 |

All 800 reads and 800 index builds were verified. One index slot improved low-load read goodput in both reciprocal pairs, but the second pair more than doubled P99. In the first ten-client pair, reads rose about 13% while concurrent index throughput fell about 35%. The candidate fails the predeclared joint throughput/tail gate and is not promoted. Operators can still set a class cap when they explicitly prefer reserved compute headroom. Benchmark source 5795961, execution checkout 609172a as recorded in the raw file. Reproduce with `python -m benchmarks.hera_db_isolation --slot-comparison --output out.json.gz`. [Per-read and per-index results](raw/index_slots.json.gz)
