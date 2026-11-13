# Same-branch projection write cost on PostgreSQL 17

The old REST lifecycle ran with no branch counter configured. Migration 0026 still called its PL/pgSQL function on task transitions, but those calls had no shared counter tuple to lock. Configured branch counts introduce a different contention mechanism.

## Evidence

Four persistent writer connections, 48 task rows with disjoint writer ownership, one branch, PostgreSQL 17.11, fsync enabled, one CPU (8) for the separate client and PostgreSQL processes. Each UPDATE and its commit are individually timed. A separate observer samples active database waits every 5 ms. The raw files include the nested statements from pg_stat_statements, commit intervals, WAL LSN deltas, EXPLAIN ANALYZE triggers, per-function times and the observer samples. Validation reads in the queue arm used one MVCC snapshot to check both pending deltas and authoritative fallback. Folding deletes only the selected queue rows and advances the checkpoint in that same transaction. A sequence high-water mark alone cannot establish that all earlier transactions committed. This is a closed-loop state-transition experiment with already-created tasks. It excludes task-command processing, role checks, Outbox delivery outside the projection, HTTP and model work.

| Each arm × 960 commits | Writes/s, forward / reverse | Trigger ms per call | WAL bytes/write | Samples with a lock wait / all samples |
|---|---:|---:|---:|---:|
| Raw task table | 2659 / 1011 | 0 / 0 | 343 / 322 | 0/59 · 0/120 |
| 0026, branch disabled | 2111 / 684 | .070 / .276 | 320 / 321 | 0/72 · 0/166 |
| Fused update, branch disabled | 1709 / 943 | .070 / .296 | 322 / 327 | 0/77 · 0/107 |
| 0026 synchronous hot counter | 1419 / 1008 | 1.313 / 1.699 | 608 / 606 | 97/108 · 115/131 |
| Fused synchronous hot counter | 1075 / 1364 | 1.556 / 1.293 | 461 / 462 | 115/129 · 99/111 |
| 16 counter shards | 1292 / 1349 | .134 / .111 | 405 / 428 | 0/85 · 0/95 |
| Append delta, then fold | 2068 / 1308 | .074 / .138 | 588 / 588 before fold | 0/69 · 0/105 |

The fusion reduced WAL with inconsistent throughput under contention. The shard and delta prototypes measure the two-state fixture only. Their totals matched the task table after every arm and both queue drains. The delta WAL comparison excludes the additional fold transactions recorded separately in the raw file. Neither prototype changes the production projection or recovery path. Host variability is visible even between the raw arms. [All runs](raw/projection-writes/transitions.json.gz) · [Driver](../../../benchmarks/hera_progress_writes.py)

## Opt-in maintenance suspension

On a database with **zero configured branch projections**, operators can now use `python -m scripts.workflow_projection suspend-maintenance`. It checks emptiness while holding the task configuration lock and exclusive projection-table lock. The same transaction protects the empty table with a CHECK constraint and disables only the named progress trigger. An older binary trying to rebuild while maintenance is suspended receives a database constraint error, preventing silent stale counts. `resume-maintenance` restores the old trigger. Rebuilding a branch with the updated operator command automatically resumes first and backfills from authoritative task rows. Use resume before rolling back to older branch administration tools. Switching requires the database table owner and takes relation locks until commit. No schema migration or data copy is needed. The ordinary task path never checks this switch.

In a separate ABBA/BAAB run with 2,400 commits per arm, baseline → suspended throughput was 1231 → 1701, 1935 → 1924, 1550 → 2781, and 1405 → 2195 writes/s in adjacent pairs. P99 fell in all four pairs (13.05 → 10.88, 10.40 → 5.37, 14.02 → 4.99 and 27.70 → 9.73 ms). Baseline charged .066–.146 ms of summed function time per transition; suspended had exactly zero calls. No writer lock waits were sampled with an empty table. Clock-time gains are variable, and summed function elapsed time is distinct from end-to-end throughput. The 5–6% historical REST regression cannot be attributed to a configured-branch write lock. The trigger's contribution to the full lifecycle requires its own paired lifecycle arms.

Only operators explicitly opting in change behavior. The regression exercised transactional rollback, attempts to insert a counter directly while suspended, automatic backfill after real task creation, refusal with a live counter, and rollback of the switch. Two PG integration tests passed. The first fixture run lacked a sufficient story-time grant and was corrected before rerun. [Raw ABBA/BAAB](raw/projection-writes/empty-switch.json.gz) · [PG regression](raw/projection-writes/guard.xml) · [Initial fixture failure](raw/projection-writes/guard-fixture-failure.log)

Replay in a disposable PG17 cluster with pg_stat_statements preloaded, using a login allowed to create an isolated database, alter its statistics settings and create the extension. Run the commands under an explicit affinity shared by all participating processes.

```sh
python -m benchmarks.hera_progress_writes --operations 240 --output transitions.json.gz
python -m benchmarks.hera_progress_writes --order disabled,suspended,suspended,disabled,suspended,disabled,disabled,suspended --operations 600 --output empty-switch.json.gz
python -m pytest -q tests/integration/test_hera_progress.py
```
