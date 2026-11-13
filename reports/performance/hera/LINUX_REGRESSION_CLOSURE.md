# Linux PostgreSQL regression closure

The final Linux inventory of 1,078 pytest node IDs was collected on `0a26cb80b82f7d4204a2e87ec5edb98f39a8c77a` with the separate-cluster S6 module enabled. The combined JUnit contains **1,075 distinct passing tests, 3 platform-specific skips, 0 failures, 0 errors and 0 missing node IDs**. Seventeen pytest subtests from the original shard are excluded from the node-ID count. The Windows-specific tests cover native host executable selection, write-through publication, and native physical resource monitoring.

Tests were verified in shards and focused reruns across the following frozen snapshots. This is a union of observed coverage across source snapshots, not a single uninterrupted run of the final Git commit. The test-only change at 0a26cb8 shares the product code from e3cd05a. The source snapshots differ in quota-row reclamation, disaster-recovery fencing, the transaction exception mapping and the progress administration isolation check. Changed paths were additionally exercised after their fixes. Pending remote CI on other commits cannot be inferred from this local run.

| Source of selected observation | Number of distinct node IDs | Coverage |
|---|---:|---|
| f0fcacd | 806 | Original independent PG17 Linux shards with compact create enabled |
| bb31d07 | 75 | Focused product checks and the remaining heavy-integration shard |
| 69d345a | 28 | Sixteen capacity tests, eleven workflow tests, real dump/restore with Provider accounting |
| f3b0a7b | 3 | Progress projection including concurrent writes against an administrator's stale snapshot |
| 0a26cb8 | 166 | Fresh bounded-group completions, four failed environment setups, S6 dual cluster and both auth races |

The original shard 2 failed during PostgreSQL setup because the temporary filesystem ran out of space. All four affected S9 tests passed on a fresh PG17 cluster with data and temporary files on the workspace filesystem. Shard 3 terminated with exit 137 before publishing a JUnit. Its node IDs were individually recovered through smaller runs. The S6 three-arm engineering acceptance ran on two separate PostgreSQL clusters and passed.

The original race test injected revocation by wrapping `TaskService.create`. Compact create takes a different entry point, so the intended revocation never happened. The replacement preserves the former callback for the original path and revokes after TaskSpec validation for compact create. Both now check that the separate revocation transaction committed, the public call returned VIEW_DENIED, and the denied create wrote no task, idempotency receipt or corresponding Outbox row. Both variants passed.

[Deduplicated JUnit](raw/linux-closure/nodeids.junit.xml.gz) · [Every observed verdict and source](raw/linux-closure/observations.json.gz) · [Original JUnits, complete collection, failed-shard logs and reconciliation script](raw/linux-closure/source-junits-and-diagnostics.tar.gz) · [SHA-256 for archived sources](raw/linux-closure/archives.sha256.json)
