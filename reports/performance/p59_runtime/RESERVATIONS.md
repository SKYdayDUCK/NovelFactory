# Task budget reservation on PostgreSQL 17.11

A fresh reservation used four SQL aggregates under the task and budget locks to read the same indexed Attempt history. Conditional sums now take a single snapshot of outstanding/resolved money and tokens. Monetary refusal retains its precedence over token refusal. UNKNOWN and PREPARED keep their reservations and settled FAILED calls contribute their actual charge. The change does not modify budget rows, transaction boundaries or settlement triggers.

The matched workload calls the production TaskService with the same role fixture, a two-connection pool, fixed charges, token limits and an isolated PG17 server with fsync and synchronous commit on. Every sequence starts with a new book, task and budget, and increments unique call keys. The fixture creates PREPARED, SENT, UNKNOWN, SUCCEEDED and FAILED in equal proportions with no external provider. The timer includes the complete database transaction, excludes fixture settlement, and counts SQL through the SQLAlchemy execution event. The baseline is main at `434840593179e6c1faa6b7aeca3d7d747644506d`.

| Measured prior ledger size | Run order | Baseline median / P95 ms | Candidate median / P95 ms |
| --- | --- | --- | --- |
| 30 through 89 | A1 B1 B2 A2 | 5.616 / 7.106 and 7.036 / 12.175 | 6.918 / 14.268 and 5.668 / 13.212 |
| 500 through 599 | B1 A1 A2 B2 | 7.007 / 11.628 and 7.386 / 15.256 | 6.326 / 14.207 and 6.456 / 12.179 |

For every measured new reservation the full transaction fell from 15 SQL statements to 12, including an aggregate count reduction from four to one. The longer-history medians improved on both repeats. The short-history median ordering and tail latencies varied on the shared host. These measurements apply to the spending critical section, and do not establish a workflow RPS change. Individual raw observations and source SHA-256 identities are in [the raw directory](raw). The [spending regression cases](../../code_review/p59_runtime/raw/scheduler-spend-linux.xml) exercise independent task and pooled limits, zero/unknown charges, cancellation races, settlement and idempotency.

## Reproduction

Use a detached worktree at the baseline SHA beside the candidate checkout and the locked root environment. Start a disposable migrated server with `scripts/with_postgres.py --migrate -- ...`. Inside it run `benchmarks/runtime_reservation.py --history 30 --samples 60 --output output.json` with `PYTHONPATH` set to the baseline or candidate absolute `src` directory. Repeat in the listed order. Use 500 warm-up rows and 100 measured rows for the long-history set. Use fresh writable output paths. Fixed alternating state transitions occur after each successful reservation. Reverting the aggregate restores the earlier query path with no migration.
