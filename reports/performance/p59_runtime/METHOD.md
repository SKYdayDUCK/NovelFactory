# Reproducing the workflow and throughput measurements

## What the counters mean

The workflow driver provisions a new role grant, principal, task, compiled input and enrollment for each participant through the existing application services. Four provisioning threads alternate between two books. Before draining, the driver records ready, blocked and cancelled populations and reconstructs the supervisor from PostgreSQL. Two accepted prerequisite summaries make the waiting tasks eligible for reconciliation. Then the ordinary admission and result-delivery paths drain the queue. One participant does not imply one simultaneous worker, open connection or model request.

The adapter uses an authorized task context and sleeps for 5 or 100 ms. Each hundred tasks include one deliberate cancellation before dispatch and one lost reply after send. It returns a task result envelope bound to that task and context to the application through the same adapter interface as other providers; its simulated receipt is never used as commercial billing evidence. The correctness verifier reads task generation, execution count, current result disposition and bound context, workflow Inbox receipts and the Attempt ledger from PostgreSQL, and checks them against the adapter invocation count. Only `ACCEPTED` and `RESULT_STORED` contribute to result throughput. The two prerequisites are eligible for summary acceptance. The world head of both books must be unchanged. The timing window includes creation and reconciliation for the end-to-end rate and excludes initial provisioning for the drain rate. See the [independent review of the driver and selector](../../code_review/p59_runtime/workflow-load-review.md).

| Boundary | Observation and scope |
| --- | --- |
| Creation and offered rate | The workflow records each provision and enrollment. The HTTP load records planned, emitted and completed operations. Its open-arrival record carries the schedule, coroutine dispatch, HTTPX header-send start and network completion clocks. The HTTPX trace marks a local send attempt. It cannot measure the time the peer NIC accepted a packet. |
| HTTP receipt, authorization, operation budget | The HTTP process counts ingress admissions, the independent authentication limiter, and the business limiter. Stage snapshots attach queue wait and inclusive timings to each scope. A `429 BACKPRESSURE` is an admission result. Genuine command conflicts have `409 DEPENDENCY_CONFLICT`. Raw per-request records distinguish a validated response, an error envelope and transport failure. |
| Database | Actual checkout events measure the shared workflow pool. HTTP runs also report connection acquisition, cursor time, transactions, connection release at each phase, WAL and sampled activity. The observer uses one separate PostgreSQL connection. A sum of per-process peaks can overstate simultaneous occupancy. |
| Dispatch and model | PostgreSQL timestamps task creation and its first RUNNING outbox event. Their difference includes dependency waits and the up-front provisioning of other agents. The benchmark counts admitted futures, active adapter invocations, sent outcomes and receipts separately. Runtime timing starts before admission and ends when the driver observes the completed future. It includes the simulated provider wait and observer scheduling. |
| Response and effective work | The HTTP clients validate the output and report both all responses and verified business successes. The deferred-validation open-arrival benchmark records network duration and client validation separately. The workflow produces application results without HTTP or MCP traffic. Protocol rates, execution concurrency and workflow goodput have separate denominators. |

## Commands

Use a clean checkout with the locked root environment installed, a native PostgreSQL 17 binary directory in `PG_BIN`, and fresh writable output paths. The wrapper creates and tears down a disposable cluster with checksums, fsync and synchronous commit enabled. The following commands were run on Linux. PostgreSQL, Python and benchmark versions are embedded in the raw results.

```bash
export PATH="$PWD/.venv/bin:$PATH"
export NF_POOL_SIZE=8 NF_POOL_OVERFLOW=0
out=$(mktemp -d)
for n in 500 1000 2000; do
  python scripts/with_postgres.py --log-dir "$out/pg-$n" -- \
    python -m benchmarks.p59_workflow_load --agents "$n" --output "$out/workflow-$n.json"
done
python scripts/with_postgres.py --log-dir "$out/pg-plan" -- \
  python -m benchmarks.p59_workflow_load --agents 2000 \
    --dispatch-profile "$out/plans" --output "$out/plan-results.json"
python scripts/with_postgres.py --log-dir "$out/pg-soak" -- \
  python -m benchmarks.p3_stability --agents 500 --seconds 3600 --read-rps 20 \
    --api-processes 2 --renew-worker-leases --output "$out/active-read-hour.json"
```

Omit `--renew-worker-leases` to reproduce the original read fixture. The renewal variant uses the existing task owner's lease token and generation, and requests the original 3600-second lease every 20 minutes. The driver counts renewals separately. Service processes, payloads, fixed read cuts, expected outputs and offered rate stay the same. A one-hour read run covers persistent API and DB behavior. The mixed workflow drains and process-loss recovery suites exercise separate conditions. The [evaluation guide](../../../docs/en/10-performance-and-evaluation.md) has the unchanged 200-agent and 1000-agent AT-30 commands.

## Capacity beyond the measured population

At 5,000 participating agents a bounded shared-process installation still allocates execution and model resources to the admitted subset. Dependency waits need durable rows and indexed reconciliation, with a bounded active execution/connection budget. As bursts grow, backlogs can grow even with a constant number of active workers. The first-claim query, serialized fairness arbitration, work/organization sharing, per-generation grant checks, per-budget locks, growing Attempt history, and object-store writes each have different scaling behavior. A supervisor also requires a LISTEN connection outside its worker pool. Separate stdio processes and separate application processes have independent pool limits. Aggregate budgets therefore depend on both process count and per-process limits. A stale eligible branch ticket can delay other branches up to its expiry. Commercial latency, per-provider quota and unknown billed requests constrain how many concurrent sends can be safely admitted.

5,000 active workflows, 5,000 stdio processes, high-rate HTTP success at original AT-30 targets, multi-hour mixed work, cross-host storage and provider callback resumption each need separate measurements. None is extrapolated from the 500–2,000 workflow drain or the single-host PG17 plans. A deployment raising concurrency should measure ready-to-claim time, fairness share, aggregate database connections, ledger contention and validated results on its own topology before increasing admission budgets.
