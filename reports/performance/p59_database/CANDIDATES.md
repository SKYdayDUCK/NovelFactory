# Task-status and lease experiment

The measured read is the complete authenticated-principal business operation behind `GET /api/v1/tasks/{id}`. Every observation contains the current authorized task and the immutable input WorldRef for its sampled generation. The other operation renews one trusted supervisor lease through the original TaskService transaction. This driver does not measure HTTP authentication, socket throughput, MCP throughput or end-to-end story workflows. Benchmark results for those layers have their own entrypoints.

| Mode | Read implementation | Scheduling | Strong transactions |
| --- | --- | --- | --- |
| baseline | Original Kernel.get_task, four business SELECTs for a regular active grant | Bounded caller concurrency; four executor slots | Independent TaskService.renew |
| queues | Original Kernel.get_task | Two bounded logical lanes, round robin by dispatch; up to four parallel operations | One renewal per dispatch |
| sql | Kernel(joined_task_status=True).get_task, current task joined to its input on (task_id, generation); original work and grant checks | Same bounded caller execution as baseline | Independent TaskService.renew |
| micro | Set-wise task rows, sorted SHARE-locked work rows, sorted SHARE-locked grant rows and generation-pinned input rows, each as its own SELECT | FIFO, parallel batches of adjacent compatible status reads | A renewal breaks the FIFO read batch and runs alone |
| micro_queues | Same four set-wise reads as micro | Alternating nonempty read and renewal lanes | One renewal per dispatch |
| hybrid | Set-wise joined task/input SELECT, then the two sets of live authority rows | Same two-lane parallel coordinator as micro_queues | One renewal per dispatch |

The three coordinated modes using batching validate each principal, work, role capability, branch and owner against PostgreSQL on execution. Each result has its own Future and its own WorldRef object. Error results are correlated by the request's position. A transaction or connection failure discards the entire read batch. Revocation committed before the authority lock is acquired takes effect even if the task row was previously sampled. The frozen incumbent validators run on the locked work/grant rows including live resume fences. Task status discloses no historical, reader, belief, provider or model-budget content. There is no generic SQL submission interface.

The executor accepts at most `capacity` waiting tickets and at most `workers * batch` additional tickets in a collected or running batch. The worker count may not exceed the common application pool size. A single read facing an empty queue does not wait to fill a batch. Under pressure, consecutive ready reads coalesce; a partial batch may wait for the unspent portion of its configured window, also capped by its earliest request deadline. Dispatch alternates nonempty lanes so a hot read lane cannot indefinitely postpone a ready lease. A timed-out queued operation is discarded. Timing out after sending a lease returns OUTCOME_UNKNOWN. The coordinator never retries that operation. Python caller cancellation discards the waiting Future and an already-dispatched database transaction completes normally.

Run from the checkout with the project's dependencies, a test-owned PostgreSQL 17 cluster and `NF_TEST_DATABASE_URL` for a role with CREATEDB. Each invocation creates, migrates and removes its own database, object store and fixture identities. The separate observer uses one administrative-database connection. With pg_stat_statements already installed and preloaded in that database it records before/after query statistics scoped to the trial database OID. A server without the extension still records cursor executions, transaction timings, application connection occupancy and sampled pg_stat_activity. SQLAlchemy's cursor count excludes pool pre-pings, transaction framing and network protocol messages. The optional server-side counters include statements the driver sends for those purposes.

```bash
python -m pytest -q tests/integration/test_p59_db_candidates.py
python -m benchmarks.p59_db_candidates --mode baseline --workers 4 --concurrency 64 \
  --agents 50 --requests 600 --write-every 10 --batch 32 --window-ms 2 --output baseline.json
python -m benchmarks.p59_task_http --count 320 --concurrency 16 --output task-http.json
```

For the six-way comparison change only mode and output path, run all six serially and repeat in reverse order. Use distinct output paths for every run. For batch sensitivity vary `--batch` across 1/8/16/32/64/128 and `--window-ms` across 0/1/2/5/10 at the same logical concurrency. Test sparse traffic separately with concurrency 1. For a read-only workload set `--write-every 0`. Other positive values schedule a renewal on the first and then every Nth operation. The two works each have an independent live lease, so a high write ratio creates real row contention. Every logical reader has a distinct grant and task; the expected result is obtained via the frozen incumbent before dispatch. Every response is checked, including its world and generation, and the latest acknowledged lease expiration is compared to persisted state after drain. Independent pending-queue saturation, timeout, cancellation, unknown write outcome, revocation and database failure tests live in the PostgreSQL integration suite.
