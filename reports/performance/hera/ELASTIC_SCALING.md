# Compute elasticity under a fixed database budget

PostgreSQL 17.11, Linux x86-64, Python 3.12.14. A/B/B/A compared two fixed process workers against the existing opt-in adaptive controller with a two-worker ceiling. A run performs 24 real resource transfers at fixed-shape world revisions, checks the final index watermark and times the authorized index drain. The input is a simultaneous backlog. Both arms have a three-connection parent pool, one connection per spawned child and two globally admitted slots. No model call occurs.

| Run | Fixed A1 | Adaptive B1 | Adaptive B2 | Fixed A2 |
|---|---:|---:|---:|---:|
| First A/B/B/A, jobs/s | 36.79 | 36.49 | 31.11 | 41.82 |
| First P99 completion, s | .652 | .658 | .771 | .574 |
| Repeat with per-event timings, jobs/s | 20.18 | 11.44 | 16.16 | 26.69 |
| Repeat P99 completion, s | 1.189 | 2.097 | 1.485 | .899 |

Both adaptive arms in the first block grew from one to two workers. All 192 measured index operations across the two blocks completed. Adaptive mode lost throughput in both pair orderings in both blocks. Actual host load differed between blocks. Leave adaptive scaling opt-in. Workload-specific lower bounds, alternative fairness and routing policies need their own measurements. The supervisor recovery tests check idle-pool retirement/reconnect and that a stop/downscale can preserve an in-flight call and leave queued jobs available to a new worker.

Replay with `python -m alembic upgrade head` and `python -m benchmarks.p55_capacity --jobs 24 --workers 2 --output out.json` on a disposable PostgreSQL 17 endpoint in `NF_TEST_DATABASE_URL` and `NF_DATABASE_URL`. Source for the detailed replay is `19718eb`. [First block](raw/compute_elastic.json) · [Second block with 96 individual source-event receipts](raw/compute_elastic_detailed.json) · [Original matrix and gates](RESOURCE_EXPERIMENT_PLAN.md)
