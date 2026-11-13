# Two-replica mixed-transport status reads

The [preregistered protocol](ROUTING_PLAN.md) used commit `d00d1ab` in a detached checkout, real local PostgreSQL 17 and two books. Each trial checked 128 task IDs, current generations, states and WorldRefs through normal SDK and REST entry points with real identity bindings. The load generator and each server ran in separate OS processes. Both configurations had four database connections, two authentication slots and two operation slots in aggregate, with zero overflow. We preserved all 2,048 request records across 16 [frozen trials](raw/routing). Every response matched. The [development-worktree run](raw/routing-development) is archived separately; concurrent edits in that checkout limit exact source attribution for spawned children.

One server hit its four-connection limit and two application slots in all four saturation trials. On raising closed-loop concurrency from 8 to 32, reciprocal throughput pairs changed from 49.88 to 51.32 and from 54.39 to 57.86 reads/s. Peak queued operations grew from 6 to 30. Both comparisons met the preregistered single-server plateau condition.

| Host routing policy | Correct reads/s, A/B/B/A | Gain vs adjacent single | P95 ms, A/B/B/A | P99 ms, A/B/B/A |
|---|---|---|---|---|
| rr | 53.92 / 93.88 / 81.64 / 58.38 | +74.1% / +39.8% | 804 / 545 / 468 / 692 | 818 / 585 / 515 / 708 |
| least | 58.91 / 90.79 / 87.55 / 61.13 | +54.1% / +43.2% | 783 / 434 / 417 / 601 | 796 / 452 / 444 / 620 |
| p2c | 59.04 / 91.86 / 88.31 / 62.8 | +55.6% / +40.6% | 772 / 513 / 412 / 605 | 789 / 525 / 423 / 624 |

A denotes one process with four connections. B denotes two processes with two connections each. At both process counts exactly 16 of 32 principals used MCP and the other 16 used REST. Two-replica routing warmed two independent SDK sessions per MCP principal to measure request-level routing. A single process warmed one each. Distinct SDK sessions did not carry mutable model context. Client-side outstanding counts were observed in one host. Aggregate SQL pool peaks on B are the sum of two per-process peaks; this does not establish simultaneous checkout. The per-process RSS, CPU, SQL and admission counters are included in every raw file. More processes consume more memory; these experiments fix aggregate DB/operation capacity and use a shared Linux host without per-server CPU pinning. Host peak RSS may be inherited across process starts and is only an upper-bound memory indicator.

All three hosts passed both reciprocal throughput and tail gates in this specific read workload. With only two distinct candidates, power-of-two choices observes exactly the same pair as least outstanding; trial-to-trial differences are timing variation. The experiment compares the topology and host routing together and does not isolate an advantage of a particular routing algorithm. The production server stays independently deployable. HTTP/MCP request quotas spanning independently administered load-generator hosts, heterogeneous expensive commands, multi-host failover, and lower-load regressions must be evaluated before making multi-replica routing a deployment default. No service middleware or automatic production routing was installed.

Reproduce against a disposable PostgreSQL 17 administration URL:

```sh
python -m benchmarks.hera_routing --output /tmp/hera-routing
```

Return to one process with a four-connection pool and the same authorized binding roster to revert the trial topology. Reads leave no durable model calls. Real task receipts and provider attempts remain in the shared PostgreSQL database.
