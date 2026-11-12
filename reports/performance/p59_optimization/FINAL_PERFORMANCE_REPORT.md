# P5.9 performance verdict

**Select the scope-upsert candidate for guarded writes. Leave AT-30 open.** A four-scope commit needs one ordered conflict statement in place of four statements. Single-target, empty, historical and rollback behavior were exercised on PostgreSQL 17.11. The paired measurements, including individual losses, are available in [BEFORE_AFTER.md](BEFORE_AFTER.md). Whole precommit median at 100 items fell from 72.403 ms to 68.348 ms. Scope-only median at 256 keys fell from 145.909 ms to 6.984 ms.

At 200 overload, the baseline produced 40.08 wire starts/s and the candidate produced 27.80/s on another two-core assignment under shared load. Both completed the bounded business-recovery gate and remained far below the original target. The wire rate is an observed regression between runs. The differing host contention precludes assigning that difference to scope batching. At 1000 open read, the baseline validated 1319 out of 5000, with 3627 status 429s and 54 transport errors. The candidate open-read rerun validated 1434 of 5000 with 3534 responses at 429 and 32 transport errors, achieving 38.26 wire starts/s. See [all completed runs](CAPACITY_RUNS.md) and the [bottleneck analysis](BOTTLENECKS.md).

The separate 500-agent 600-second read test completed all 12,000 queries, with P50/P95/P99 of 199.65/1071.38/1519.10 ms and zero correctness errors. Its one-hour gate remains NOT_EXECUTED.

A separate [validation experiment](VALIDATION_EXPERIMENT.md) compared first-error evaluation and native-only copy avoidance against the full contract registry. Both stayed experimental. The independent repeat of the 500-agent read test also completed 12,000/12,000 requests with P99 996.15 ms.

The [operation matrix](OPERATION_MATRIX.md) covers 16 service boundaries with actual PG17/Redis, stdio and loopback HTTP measurements. Four tenants with three tasks each shared one model slot, completed all 12, and accumulated three virtual-finish units each. Real commercial inference, settled bills and literary scoring were outside these experiments.

Source baseline, isolates, SQL counts, PG plan, statement-level execution/WAL, allocation and CPU profiles, percentiles, original large-load outcomes, failures, full argv, and environment records are included under raw/. [Reproduction](REPRODUCE.md)
