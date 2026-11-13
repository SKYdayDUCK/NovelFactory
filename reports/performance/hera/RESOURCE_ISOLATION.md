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

## Project progress while both model slots wait

Four separately migrated PostgreSQL 17 fixtures each enrolled two model tasks in each of two books. Four distinct branch-scoped project grants sent 20 reads each; each book had one REST workflow client and one MCP SDK 1.29.0 client calling `nf_project_status`. The optional workflow-count projection was disabled. A pre-existing cancelled task in the second book made the branch totals distinguishable. Every one of the 320 complete responses matched its own grant's authorized progress snapshot, including the grant expiry. Independent SQL state counts matched each snapshot. No task, lease or attempt changed between the before/after snapshots of any measured interval. These are warmed loopback observations from a shared host with the client and server in one process. [Preregistered workload](RESOURCE_EXPERIMENT_PLAN.md) · [Per-request timing, fingerprints, branch/grant IDs, complete snapshots and SQL counters](raw/progress_model_isolation.json.gz)

| Arm in order | Correct progress reads | Correct reads/s | P95 ms | P99 ms | Peak DB connections | In-flight SENT / queued READY model tasks |
|---|---:|---:|---:|---:|---:|---:|
| Quiet A1 | 80/80 | 27.11 | 187.70 | 274.31 | 4 | 0 / 4 |
| Loaded B1 | 80/80 | 26.94 | 220.85 | 262.60 | 4 | 2 / 2 |
| Loaded B2 | 80/80 | 17.55 | 464.72 | 708.75 | 4 | 2 / 2 |
| Quiet A2 | 80/80 | 32.27 | 152.56 | 189.69 | 4 | 0 / 4 |

In each loaded interval, one task per book stayed in provider generation with a SENT attempt and a live model dispatch lease. One eligible task per book remained READY. At provider entry the pool had zero checked-out connections. After release, all eight jobs across the two loaded fixtures reached RESULT_STORED, with one provider call and one durable attempt per task and zero remaining dispatch leases. The provider was an in-process deterministic fixture.

The first adjacent pair lost 0.62% goodput; P95 rose 17.66% and P99 fell 4.27%. The second pair lost 45.62% goodput; P95 rose 204.61% and P99 rose 273.63%. Authorized progress stayed readable under the observed backlog. The second pair crosses all three preregistered 20% degradation limits. This small single-host run cannot establish a latency guarantee or attribute the difference to a particular component. The separate 5,000-task projection benchmark tests a different question. To replay from frozen source `20801bc`, set `NF_TEST_DATABASE_URL` to a disposable PostgreSQL 17 administration connection and run `python -m benchmarks.hera_progress_model_isolation --output progress_model.json.gz`. [Driver](../../../benchmarks/hera_progress_model_isolation.py)

## Repeated index-materialization and OS-priority experiments

All processes, including PostgreSQL 17.11 and the mixed MCP/REST load generator, inherited Linux CPU 8 affinity. Host CPU time and filesystem I/O were shared with other workloads. Each arm populated two reader books and a third branch with 60 actual accepted transfers. Eight independent owners issued 128 exact task status reads. Every loaded arm ran two workers, drained 60 distinct receipts, and verified the authoritative final timeline watermark. API business/authentication slots stayed at 2+2. Six application/worker connections and an administrative observer remained available. The metrics below start at the client release and include all read outcomes. Index drain includes work after the read interval.

| Source / topology in execution order | Correct reads/s | P95 / P99 ms | Index completions while reading | Index/s while reading / through drain |
|---|---:|---:|---:|---:|
| Original / quiet | 53.16 | 204 / 254 | 0 | 0.00 / 0.00 |
| Original / shared | 31.36 | 343 / 411 | 37 | 9.06 / 10.79 |
| Compact index / shared | 26.78 | 388 / 443 | 40 | 8.37 / 10.77 |
| Compact index / shared | 22.79 | 709 / 1027 | 41 | 7.30 / 9.49 |
| Original / shared | 27.73 | 421 / 746 | 35 | 7.58 / 9.83 |
| Original / quiet | 46.79 | 236 / 298 | 0 | 0.00 / 0.00 |
| Compact index, second block / quiet | 50.09 | 251 / 287 | 0 | 0.00 / 0.00 |
| Compact index, second block / process | 29.55 | 354 / 383 | 39 | 9.00 / 11.71 |
| Compact index, second block / process-nice | 32.64 | 344 / 463 | 3 | 0.76 / 8.19 |
| Compact index, second block / process-nice | 34.35 | 391 / 470 | 2 | 0.54 / 9.04 |
| Compact index, second block / process | 23.70 | 471 / 898 | 41 | 7.59 / 9.35 |
| Compact index, second block / quiet | 49.32 | 228 / 307 | 0 | 0.00 / 0.00 |

The index candidate removed duplicate canonical encoding and deep copies, rechecked only the three immutable revision witness fields at commit, and advanced both watermarks in one write. The standalone 60-job cProfile run used 1,800 SQL calls before the change and 1,620 after. Cumulative deep-copy time fell from 0.548 to 0.006 seconds. Profiled elapsed rose from 4.194 to 5.204 seconds. The two shared-pool loaded comparisons both lost read goodput, with no joint improvement in index throughput. The candidate stays out of production. The raw cProfile rows include overlapping cumulative times and instrumentation overhead. Fifteen PostgreSQL regressions passed, including interrupted publication, stale tokens, replay, role visibility and lock ordering.

The second block exercised the candidate under the same CPU and total pool budgets. Only the worker process received Linux nice +10 for process-nice. PostgreSQL, API and client remained at their original priority. In both treatments very little indexing progressed while the reads were in flight. Nice +10 also missed the 100 ms read target. Do not deploy it as the fast-path default.

Replay against an isolated PostgreSQL 17 admin URL in `NF_TEST_DATABASE_URL`. Pin PostgreSQL and every Python process to the same CPU. At incoming `eb1a75e`, run `python -m benchmarks.hera_index_topology --order quiet,shared --output A1.json.gz`. Apply the archived candidate patch in an isolated worktree and run `--order shared,shared`, then restore the original for `--order shared,quiet`. With the candidate and the priority probe together, run `--order quiet,process,process-nice,process-nice,process,quiet`. The independent `benchmarks/hera_profile_index.py --output profile.json.gz` driver uses the checkout selected by `PYTHONPATH` with script execution via Python. The two shared controls straddle the candidates in time. Original, unfiltered request, SQL, OS and index records are preserved in [the qualification directory](raw/index-worker-qualification/).

On one additional 60-revision fixture, the candidate's persisted content-addressed file was read through the verifying object store and compared byte-for-byte to the original `eb1a75e` materializer on each identical source row. All 60 artifact identities, hashes and all 3,212,325 bytes matched. The original projector's `_material` and the candidate run used the same migrated database and immutable revision identifiers. Per-source SHA-256 and object lengths are in [the equality results](raw/index-worker-qualification/object-equivalence.json.gz).
