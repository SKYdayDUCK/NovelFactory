# Live authority on the single-task MCP status path

The remote opt-in `NF_HERA_FUSE_STATUS_AUTH=1` sends a schema-valid `nf_task_status` on a provisioned `TaskOperationsBinding` to the existing READ-fenced business operation. The narrower host tool ceiling applies first, and task owner, current grant, work, branch, generation and binding checks still run. The method never caches a grant. The previous extra discovery transaction read the grant, then locked work and grant once per advertised capability (READ and TASK_CREATE on these writers). Both revisions persist the start audit before dispatch, the completion audit afterwards, and an Outbox entry for each. The standard HTTP bearer check and SDK transport run for every POST. The operator restores the old path with `NF_HERA_FUSE_STATUS_AUTH=0` or by removing the setting.

## PostgreSQL and SDK ablation

Git `2ef2525529e6d22f5315c3455d59c12738a05128` (full SHA in each capture). Python 3.12, PostgreSQL 17.11, pool of four with zero overflow, two works, eight distinct live MCP SDK sessions and grants. Each arm recreated the database, seeded one task per actor, spun up a fresh Uvicorn server, initialized each session, completed eight first tool reads, then released eight closed-loop lanes for 24 reads apiece. PG and the client used CPU 6; the separately spawned server used CPU 7. No model worker ran. Host CPUs were pinned but not exclusively reserved. First-call results are process/SDK cold; the OS file cache was not flushed.

| Arm | First-read median ms (8) | Warm correct reads/s | Warm P95 ms | Warm P99 ms | SQL/call | Transactions/call | Correct reads | Committed audit / Outbox |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A1, off | 651.27 | 31.44 | 428.13 | 458.32 | 16 | 5 | 200/200 | 400/400 |
| B1, on | 312.64 | 67.16 | 160.47 | 221.25 | 11 | 4 | 200/200 | 400/400 |
| B2, on | 257.68 | 68.79 | 153.91 | 231.68 | 11 | 4 | 200/200 | 400/400 |
| A2, off | 301.36 | 49.10 | 208.84 | 277.34 | 16 | 5 | 200/200 | 400/400 |

In both adjacent pairs the fused path saved exactly five SQL executions and one transaction, and improved warm throughput and tails. Both fused warm P95 values exceed 100 ms. The wide spread between the two controls limits inference about the effect size on another host. Complete task throughput needs the independent lifecycle measurements.

| Mean per warm call (ms, nested stages) | A1 | B1 | B2 | A2 |
|---|---:|---:|---:|---:|
| Authentication | 9.57 | 4.93 | 4.99 | 5.68 |
| Business queue wait | 115.19 | 28.94 | 26.86 | 59.82 |
| Audit start, including Outbox | 18.25 | 11.55 | 11.50 | 12.18 |
| Tool discovery at invocation | 17.64 | 0 | 0 | 11.84 |
| Fenced business operation | 9.86 | 6.56 | 6.32 | 5.30 |
| Audit finish, including Outbox | 11.89 | 8.16 | 7.55 | 7.68 |

[Per-call client timings, keyed ASGI traces, SQL texts without parameters, pool checkouts, transactions to the commit or rollback hook, and ingress/auth/business queue intervals](raw/status-auth-abba). Transaction hooks exclude DBAPI commit acknowledgement. SDK decode/encode, transport, scheduling and ASGI overhead contribute to residual time and are not individually identified by the residual. An earlier instrumentation smoke attempt produced HTTP 500 during SDK initialization because the probe omitted the default ingress category. The fix restores the default argument on the benchmark wrapper. The run ended before measuring a call. [Failed probe](raw/status-auth-probe-smoke-failed.log.gz).

The integration regression covers a host ceiling of zero, grant revocation committed **after** the durable start audit, error precedence for invalid input on a revoked grant, and corresponding Outbox records. The real mixed SDK/REST regression exercises both permutations of the flag, cross-branch denial and different owners. [Four local test cases](raw/status-auth-regression.xml.gz). Keep the flag opt-in pending the full candidate gate.

Run on Linux using an isolated PG17 cluster and an administrative test URL:

```sh
taskset -c 6 python -m benchmarks.hera_mcp_status_auth --server-cpus 7 \
  --order ABBA --clients 8 --warm-calls 24 --output /tmp/hera-status-auth
```
