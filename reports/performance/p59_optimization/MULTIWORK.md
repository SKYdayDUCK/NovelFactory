# Multi-work supervisor baseline

At commit `5e322e8af8ddb4465f68f8cab84c9685832461a9` the NF supervisor scheduled two new summary tasks per book. Each case cloned an empty migrated PG17.11 template, created separate project budgets and set one slot per provider account. For mixed and outage runs the operator froze one task on each of two distinct simulated HTTP services in every book. Outage returned HTTP 503 for A throughout the run. B remained healthy. The local fixture produces synthetic response text. All operator accounts and books were disposable.

| Works | Scenario | Upstream sends [A,B] | Stored tasks | Stored/s | Tick P50 ms | P95 ms | P99 ms | Duplicate sends |
|---:|---|---|---:|---:|---:|---:|---:|---:|
| 1 | fixed | [2, 0] | 2 | 3.03 | 248.7 | 410.9 | 410.9 | 0 |
| 1 | mixed | [1, 1] | 2 | 4.0 | 215.6 | 284.3 | 284.3 | 0 |
| 1 | outage | [1, 1] | 1 | 2.32 | 214.3 | 216.3 | 216.3 | 0 |
| 2 | fixed | [4, 0] | 4 | 3.63 | 141.2 | 284.3 | 284.3 | 0 |
| 2 | mixed | [2, 2] | 4 | 4.96 | 344.5 | 405.9 | 405.9 | 0 |
| 2 | outage | [2, 2] | 2 | 2.53 | 334.8 | 413.5 | 413.5 | 0 |
| 4 | fixed | [8, 0] | 8 | 2.49 | 168.0 | 472.6 | 550.5 | 0 |
| 4 | mixed | [4, 4] | 8 | 3.86 | 280.2 | 688.8 | 688.8 | 0 |
| 4 | outage | [4, 4] | 4 | 1.65 | 286.9 | 496.9 | 640.0 | 0 |

The nine runs issued 42 requests, stored 35 task results, and attempted each task once. In all seven A outages the reservation and its original Attempt survived. Healthy fixture calls also have UNKNOWN financial status because the fixture adapter supplies an upstream receipt and tokens but no actual bill. Task completion is measured by RESULT_STORED.

Latency includes idle or blocked ticks. Each case is a finite progress check, so tail values are observations from these runs and sample counts are available in the raw JSON. The completion rate covers the interval between starting the first scheduler pass and finishing the pass with the last arrival. Registration uses the existing _job fixture, which also checks replay and an invalid allowance; its timing is not a production-only insertion metric. CPU and RSS describe the client process; RSS is the process high-water mark, so it may carry over from previous cases. PostgreSQL lock and Redis samples were not collected in this experiment.

Run from the repository root with `PYTHONPATH=src:. python scripts/with_postgres.py --migrate -- python path/to/multiwork.py --output results.json`. The earlier assignment variant assigned profiles per book; it is recorded separately.
