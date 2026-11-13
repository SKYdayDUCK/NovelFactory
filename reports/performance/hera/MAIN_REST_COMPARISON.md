# Comparable REST task lifecycle on main and HERA

**The final HERA product completed 6.0% and 5.2% fewer full task lifecycles per second than main in the new A/B/B/A.** All correctness checks passed. Both comparisons stayed within the 20% P95/P99 tolerance, but neither passed the preregistered throughput gate. An earlier candidate was faster on both of its own reciprocal comparisons. [Preregistered workload and repetition](MAIN_REST_PLAN.md)

Both series used clean detached main `bcb03637d4f01030fc95b99d1ff743b3758297ad`. Each arm ran one new Python process against a fresh PostgreSQL 17.11 cluster. The script verified the product import path and clean Git revision. Client and Uvicorn shared a process and communicated over TCP. Each application had four pooled database connections, zero overflow, two authentication slots and two operation slots. Sixteen independent grants across two works each ran four sequential tasks. HERA enabled durable quotas and fenced singleton status reads. Each lifecycle exercised legacy create, a ready-state GET, same-key create replay, claim, a running-state GET, cancellation and a final GET.

## Final product with recovery trigger and transport fixes

B used `a918a916e283988083e9ae9f891dc5f17b44de05` with migration 0026 present. Its product src tree is identical to `52f36d4`. Neither branch had a configured progress counter and the app had no remote roster. The migrated task-transition trigger still ran during ordinary task changes. The new series changed only the driver's clean-revision guard. All measured operations and assertions match the earlier series. [Four raw final-product arms](raw/main-rest-final/)

| Run | Correct full cycles/s | Correct HTTP requests/s | Request P95 / P99 ms | Cycle P95 / P99 ms | SQL statements | Combined CPU s | Peak process MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1 | 7.251 | 50.76 | 504.5 / 571.2 | 2579.2 / 2663.1 | 4288 | 8.44 | 90.1 |
| B2 | 6.817 | 47.72 | 539.6 / 617.3 | 2622.5 / 2761.2 | 4160 | 8.59 | 92.6 |
| B3 | 7.308 | 51.16 | 515.3 / 671.3 | 2471.9 / 2574.0 | 4160 | 8.00 | 89.7 |
| A4 | 7.711 | 53.98 | 486.8 / 598.9 | 2327.8 / 2509.3 | 4288 | 8.24 | 89.5 |

Final HERA request P95/P99 increased by 7.0%/8.1% and 5.8%/12.1% in the two pairs. Full-cycle P95/P99 increased by 1.7%/3.7% and 6.2%/2.6%. Each arm verified all 448 HTTP responses and 64 terminal lifecycles, peaked at four checked-out connections and closed with zero checked out. The application issued 128 fewer cursor statements per HERA arm. Cursor counts exclude SQL executed inside PostgreSQL triggers.

## Earlier candidate

B used `0fcf453a54d9791af52dbe39ee0d9ba1a4d054e3` with migration 0025. The earlier two comparisons measured 23.7% and 15.3% greater full-cycle throughput than their adjacent main controls. All 1,792 HTTP responses and 256 cycles were correct. [Four raw earlier arms](raw/main-rest/)

| Run | Correct full cycles/s | Correct HTTP requests/s | Request P95 / P99 ms | Cycle P95 / P99 ms | SQL statements | Combined CPU s | Peak process MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| A1 | 5.577 | 39.04 | 711.3 / 1014.6 | 3306.6 / 3448.3 | 4288 | 10.20 | 91.4 |
| B2 | 6.902 | 48.31 | 504.9 / 680.2 | 2546.0 / 2670.1 | 4160 | 8.22 | 92.3 |
| B3 | 6.375 | 44.62 | 681.1 / 778.4 | 3071.3 / 3089.1 | 4160 | 8.97 | 90.0 |
| A4 | 5.528 | 38.70 | 774.9 / 969.2 | 3811.1 / 3944.6 | 4288 | 10.32 | 89.9 |

The two series used different time windows on a shared host. Main's own measured throughput ranged from 5.53–5.58 cycles/s in the earlier series to 7.25–7.71 in the repetition. This design gives a within-series contrast for each product and cannot isolate the recovery trigger's contribution to the change in relative performance.

Across eight fresh clusters, all 3,584 HTTP responses and 512 complete lifecycles matched the expected generation, WorldRef, state and replay semantics. PostgreSQL showed one claim, two command receipts, three task state events and a generation-1 cancellation per task. Neither canonical branch head moved. There were zero provider attempts. The workload measures existing REST methods under a small steady backlog. Main has no directly comparable remote MCP and durable admission combination. For the separately measured cost of a larger queue, see [the 5,000-task depth study](ADMISSION_DEPTH.md).

## Reproduction

Copy `benchmarks/hera_compare_main.py` from `67921a8` outside both worktrees. For the final series, run the commands below from the A, B, B, A clean worktrees respectively, each with a fresh local PG17 cluster, NF_TEST_DATABASE_URL, the pinned environment and PYTHONPATH containing that checkout's src, workshop/src and root. For the earlier series, use its candidate source and omit --candidate-revision. The original externally run driver is also available at `7dccd00`.

```sh
python /tmp/hera_compare_main.py --arm A --candidate-revision a918a91 --output /tmp/main-rest-final/0-A.json.gz
python /tmp/hera_compare_main.py --arm B --candidate-revision a918a91 --output /tmp/main-rest-final/1-B.json.gz
python /tmp/hera_compare_main.py --arm B --candidate-revision a918a91 --output /tmp/main-rest-final/2-B.json.gz
python /tmp/hera_compare_main.py --arm A --candidate-revision a918a91 --output /tmp/main-rest-final/3-A.json.gz
```

Rollback this configuration by disabling the durable policy for new admissions and setting NF_HTTP_FENCED_TASK_STATUS=0. Keep workers able to honor earlier frozen first-start deadlines until those waits drain.
