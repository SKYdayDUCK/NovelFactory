# AT-30 overload, contract CPU and generator saturation

The original 200-agent overload in GitHub Actions on HERA eb1a75e6 recorded 42.38 HTTPX first-call starts/s, 34.49 header starts/s, a passing bounded recovery check and a failed first-attempt capacity check. Its independent 1,000-agent, 5,000-read run validated 2,060 results at 20.73/s with 2,911 HTTP 429 responses and 29 transport errors. Existing gates and samples remain available. [200-agent overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43400337313/job/131801320682) · [1,000-agent open read](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43400337130/job/131801319851)

## Product ablation on PostgreSQL 17.11

A private PostgreSQL cluster, a separate service interpreter and the original load generator shared CPU 5 for the diagnostic. The application kept eight pooled connections. Each fresh database had fifty task grants. The regular P2 scenario compiled contexts, queried fixed worlds, issued guarded proposals, retried original receipts and reconciled final world effects. The last phase offered five rounds of fifty hotspot reads at 1,000/s. All arms passed the world and bounded-recovery checks. The measured service also recorded thread CPU by stage. The four arms used the historical prebound listener without an explicit socket protocol. [Frozen rule](AT30_FINAL_PLAN.md) · [Every original request and database oracle](raw/at30-final/)

| Chronological arm | First correct / 250 | Window s | Correct first responses/s | First call starts/s | Wire starts/s | Server schema validation CPU ms/call |
|---|---:|---:|---:|---:|---:|---:|
| A1, baseline | 237 | 17.199 | 13.78 | 18.43 | 18.22 | 1.293 |
| B1, static | 250 | 15.477 | 16.15 | 18.14 | 17.74 | 0.642 |
| B2, static | 250 | 14.620 | 17.10 | 21.62 | 17.74 | 0.587 |
| A2, ordinary | 209 | 15.177 | 13.77 | 20.84 | 17.96 | 1.296 |

The two static arms delivered 17.2% and 24.2% more correct first reads/s than their neighboring ordinary controls. The CPU cost per service contract check fell by 50.3% and 54.7%. The API performs each schema check against a fresh value. The plan references only the immutable, shipped schemas and upstream Draft 2020-12 keyword handlers. It reuses traversal and reference resolution across calls, including relative cross-document references, retaining format checks and conditional validators. An unsupported schema keyword or nested scope selects the ordinary validator for the entire bundle. Set `NF_HERA_STATIC_CONTRACTS=1` on API processes to opt in, and remove it or set it to `0` to revert. The validation and grant checks still run on cache hits.

Local evidence covers 435 passing unit/contract tests, three native Windows skips and seventeen passing subtests. A separate differential run compares mutations of each R1 example against the ordinary validator and exercises fallback on dynamic references. Neither reciprocal pair approached 950/s. Full-business throughput and CPU contention need their own workloads.

Each arm's environment includes the product revision and P2 driver digest. A1 ran eb1a75e6 plus the preregistration only (b102a79). B1 ran 57deb40. B2 and A2 ran 69b81bf, which keeps the two flag modes in separate validator cache entries and adds reference-comparison tests. A1 used the unmodified executable directly. Subsequent arms set the flag only while starting the service processes; the client used its ordinary validator throughout. Direct correctness/recovery assertions, the original gate and retries were unchanged.

Use a clean worktree and a fresh PostgreSQL 17 instance per arm with `NF_TEST_DATABASE_URL` and its postmaster pinned to CPU 5. Run the same workload in A/B/B/A order, changing `AT30_API_CONTRACT_PLAN` between 0 and 1 and the output filename for each arm.

```sh
NF_BENCH_DIAGNOSTICS=1 AT30_API_CONTRACT_PLAN=1 taskset -c 5 \
  python -m benchmarks.hera_at30_contract_arm --agents 50 --profile overload \
  --hot-fraction .8 --arrival-rps 10 --open-loop-rps 1000 --rounds 5 \
  --max-attempts 5 --retry-budget-seconds 30 --gate-scope capacity \
  --service-mode isolated-process --api-processes 1 --output /tmp/at30-B.json
python -m benchmarks.hera_at30_completion /tmp/at30-B.json
```

## Separate transport calibration

The frozen P3 HTTPX generator schedules 5,000 calls using 1,000 fixture identities. The fixed 2,104-byte body matches all 237 successful first-attempt overload responses of A1. Each response is checked byte for byte. The client uses CPU 5 and the separate stub Uvicorn process uses CPU 4. This calibration runs no business code. Both layouts use loopback HTTP/1.1. The original client uses one 256-connection pool. The independent diagnostic splits the same budget across sixteen round-robin pools of sixteen connections. Pinned HTTPcore iterates connection and queued-request collections when assigning work. Smaller pools cut per-assignment search in a burst. There is no change to the original P2 or P3 capacity gate.

```sh
taskset -c 4,5 python -m benchmarks.client_calibration --client-cpus 5 --server-cpus 4 \
  --response-bytes 2104 --repeats 2 --output /tmp/transport-single.json
taskset -c 4,5 python -m benchmarks.hera_transport_calibration --client-cpus 5 --server-cpus 4 \
  --response-bytes 2104 --repeats 2 --output /tmp/transport-pooled.json
```

The recorded driver layout is identified by filename. The original synthetic schema and response-byte hash are identical across layouts. On the original socket, the single pool produced 135.68 and 121.05 HTTP-header starts/s across 36.95 and 41.48 seconds, with client CPU occupying 99.6% and 88.3% of the elapsed time. The sixteen pools produced 388.76 and 399.47 starts/s over 13.13 and 12.72 seconds. Each arm returned all 5,000 expected bodies. The smaller pools diagnose generator overhead and provide a separate way to stress the service; these fixed-byte rates give no production throughput or universal hardware ceiling.

A second two-by-two diagnostic ran on the same CPUs with an explicitly TCP listener (5242166, using the production socket protocol). The original single pool delivered 126.27 / 221.23 header starts/s over 39.85 / 22.63 seconds. Sixteen pools delivered 490.97 / 503.59 over 10.38 / 10.14 seconds. All 20,000 responses across the four new arms matched. These arms use the same size and per-arrival checks as above. The shared-host drift, visible in the original pool, limits estimates of the socket setting's independent effect. Raw output covers the complete run and includes all delays, client CPU and server CPU for each arm. [Single pool](raw/at30-final/transport-tcp.json.gz) · [Sixteen pools](raw/at30-final/transport-tcp-16x16.json.gz).
