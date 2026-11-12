# Same-state paired comparison

| Measured boundary | Items / keys | Pairs | Base ms | Candidate ms | Median change | SQL/arm | Candidate faster pairs |
| --- | --- | --- | --- | --- | --- | --- | --- |
| full_accept_before_commit_rollback | 5 | 12 | 39.597 | 37.389 | -5.6% | 60 → 57 | 9 |
| full_accept_before_commit_rollback | 100 | 12 | 72.403 | 68.348 | -5.6% | 60 → 57 | 10 |
| scope_transaction_rollback | 0 | 10 | 0.935 | 0.921 | -1.4% | 2 → 2 | 6 |
| scope_transaction_rollback | 1 | 10 | 1.827 | 1.535 | -16.0% | 3 → 3 | 9 |
| scope_transaction_rollback | 4 | 10 | 3.808 | 1.726 | -54.7% | 6 → 3 | 10 |
| scope_transaction_rollback | 64 | 10 | 36.106 | 2.995 | -91.7% | 66 → 3 | 10 |
| scope_transaction_rollback | 256 | 10 | 145.909 | 6.984 | -95.2% | 258 → 3 | 10 |

The accept timings include ordinary verification, event writes, scope updates and a fault immediately before commit. Every trial rolls back the same frozen proposal; a final actual commit succeeds and its idempotent retry returns the same receipt. A real commit flush is outside the paired latency window. Seven independently committed baseline writes including durable receipt checks are in raw/write-baseline.json. Varying task/fixture observations elsewhere in this directory cannot be used as time-paired comparisons.
