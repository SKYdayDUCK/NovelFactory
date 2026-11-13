# Comparable REST task lifecycle on main and HERA

A used clean detached main `bcb03637d4f01030fc95b99d1ff743b3758297ad` and B used clean detached HERA `0fcf453a54d9791af52dbe39ee0d9ba1a4d054e3`. The same external driver checked the imported product path and git status for each arm before work began. It launched one independent Python process and one fresh real PostgreSQL 17.11 cluster per arm. Client and Uvicorn shared a process within each arm and communicated over TCP. All four runs configured four pooled connections, zero overflow, two authentication and two operation slots. Sixteen independent owners across two works each ran four sequential task lifecycles. HERA enabled the durable quota policy and fenced single-status reads. Requests included create, ready-state GET, exact same-key replay, claim, running-state GET, cancel and final GET.

| Run | Correct full cycles/s | Correct HTTP requests/s | Request P95 / P99 ms | Cycle P95 / P99 ms | SQL statements | Combined CPU s | Peak process MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1 | 5.577 | 39.04 | 711.3 / 1014.6 | 3306.6 / 3448.3 | 4288 | 10.20 | 91.4 |
| B2 | 6.902 | 48.31 | 504.9 / 680.2 | 2546.0 / 2670.1 | 4160 | 8.22 | 92.3 |
| B3 | 6.375 | 44.62 | 681.1 / 778.4 | 3071.3 / 3089.1 | 4160 | 8.97 | 90.0 |
| A4 | 5.528 | 38.70 | 774.9 / 969.2 | 3811.1 / 3944.6 | 4288 | 10.32 | 89.9 |

HERA completed **23.7% and 15.3% more full cycles per second** than adjacent main arms. In both comparisons the full-cycle tails improved and the preregistered throughput/correctness gate passed for this workload. Across the four runs all 1,792 HTTP responses and 256 complete task lifecycles matched the expected state/generation/WorldRef and replay semantics. Each task had exactly one claim, two command receipts, three state events and one generation-1 cancellation; no provider attempts were created and both canonical heads stayed at genesis. These are caller-requested cancellations, not completed model tasks. Suboperation tail latencies and per-request records are retained [for every arm](raw/main-rest).

The measured percentages refer only to this mix on existing REST entry points, with zero model calls and a small steady queue. They cannot quantify a full-system gain including remote Tasks, Workshop or writing performance. Main did not expose the same remote MCP and durable admission contract. Shared host CPU and filesystem load were uncontrolled; reciprocal pairs provide limited repetition. The toggles for the candidate arm are independent; this comparison changes them together. The separate [5,000-task experiment](ADMISSION_DEPTH.md) isolates backlog cost and shows the additional cost of enabled durable admission under deeper queues.

[Preregistration](MAIN_REST_PLAN.md). To reproduce, save the driver from commit `7dccd00` outside both clean matched source trees. Run these four commands from A, B, B, A worktrees respectively, each against its own fresh local PG17 service with NF_TEST_DATABASE_URL set and PYTHONPATH containing that checkout's src, workshop/src and root. Use the same pinned environment in both trees.

```sh
python /tmp/hera_compare_main.py --arm A --output /tmp/main-rest/0-A.json.gz
python /tmp/hera_compare_main.py --arm B --output /tmp/main-rest/1-B.json.gz
python /tmp/hera_compare_main.py --arm B --output /tmp/main-rest/2-B.json.gz
python /tmp/hera_compare_main.py --arm A --output /tmp/main-rest/3-A.json.gz
```

Rollback this configuration by disabling the durable policy for new admissions and setting NF_HTTP_FENCED_TASK_STATUS=0. Keep workers able to honor earlier frozen first-start deadlines until those waits drain.
