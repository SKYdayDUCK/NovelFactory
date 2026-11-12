# Hosted capacity evidence

Git revision `3bd0de0fa783c5deca189874b4ee808f1950c1ee`. Last read at 2026-11-12 16:07:16 UTC. All jobs below have a terminal result.

| GitHub job | Run | Original metric | Effective completion | Job outcome |
| --- | --- | --- | --- | --- |
| [Measured 500-mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099900/job/131303974484) | 1 | Mixed capacity PASS | 2851/2851 logical operations, correctness PASS, bounded recovery PASS | success |
| [Measured 500-read-soak](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099900/job/131303975120) | 1 | 20.001 starts/s under 20/s offered load | {'200': 12000} in 600.1s; probe PASS, long-duration NOT_EXECUTED | success |
| [Measured 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099900/job/131303975192) | 1 | 39.428 wire starts/s vs 950 | 1,775/5,000 validated; {'429': 3211, '200': 1775, 'transport_error': 14}; unchanged world | failure |
| [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974089) | 1 | 164.877 call starts/s vs 950 | 536/536 logical operations, correctness PASS, bounded recovery PASS | failure |
| [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974089) | 2 | 116.803 call starts/s vs 950 | 536/536 logical operations, correctness PASS, bounded recovery PASS | failure |
| [50 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974740) | 1 | Mixed capacity PASS | 286/286 logical operations, correctness PASS, bounded recovery PASS | success |
| [50 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974740) | 2 | Mixed capacity PASS | 286/286 logical operations, correctness PASS, bounded recovery PASS | success |
| [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974745) | 1 | 46.430 call starts/s vs 950 | 2141/2141 logical operations, correctness PASS, bounded recovery PASS | failure |
| [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303974745) | 2 | 47.625 call starts/s vs 950 | 2141/2141 logical operations, correctness PASS, bounded recovery PASS | failure |
| [200 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303975044) | 1 | Mixed capacity PASS | 1141/1141 logical operations, correctness PASS, bounded recovery PASS | success |
| [200 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099539/job/131303975044) | 2 | Mixed capacity PASS | 1141/1141 logical operations, correctness PASS, bounded recovery PASS | success |
| [Independent load process / 200 / overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099717/job/131303974028) | 1 | 32.650 call starts/s vs 950 | 2141/2141 logical operations, correctness PASS, bounded recovery PASS | failure |
| [Same world CPU and SQL paired comparison](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099717/job/131303974209) | 1 | CPU/SQL paired comparison | Workflow test passed | success |
| [Independent load process / 200 / mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43253099717/job/131303974818) | 1 | Mixed capacity PASS | 1141/1141 logical operations, correctness PASS, bounded recovery PASS | success |

For mixed and overload scene workloads, logical-operation completion after retries is separate from sustaining the offered rate. In the 1000-agent open-arrival read benchmark, each scheduled query has one attempt. The artifact digests, HTTP counts, job steps and per-operation summaries are in the accompanying JSON.
