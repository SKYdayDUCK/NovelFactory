# P5.9 integrity and historical capacity checks

## Root suite and migration

The first hosted P1/P2 integration job on commit `9bc6b79df1e22f988602cd8245bc42145aaccf58` completed with **1 failed, 1005 passed, 21 skipped and 17 passed subtests**. Its only failure was the package validator. On an independent checkout of the same tree, the validator identified one absent link target, `raw/scheduler-spend-linux.xml`. Commit `408825f` added the referenced evidence. The validator subsequently returned PASS and the same pytest case passed in 2.61 seconds. [First hosted integration job](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43273075639/job/131374253443) · [Isolated validator case](raw/integrity-validator-linux.xml)

The earlier local root-suite invocation launched `../NovelFactory/.venv/bin/python` while its working directory was a different checkout, `c7e6924f1cf9cf7799d78ab4b8743960e8571062`. It finished with **7 failed, 1016 passed, 3 skipped and 17 passed subtests**. Five process tests failed with service responses. PostgreSQL logged queries for the intermediate retry column at 17:51:31 UTC and its replacement deadline table from 17:51:37 UTC. Neither existed in the frozen checkout. A sixth failure occurred before Workshop stdio initialization, when the development runtime imported a function from the older checkout's `agent_runtime.host_process`. The seventh was the missing report link above. The return code and failed nodes of the original run are preserved. [Triage record](raw/integrity-triage.json)

The independent rerun used local commit `e23b8e0ffa4efaba314e82fcb1a5af326e80dfee`, identical in tree to hosted `ce74f265b9072df52e9a485c6dfd48d5612d1907`. The interpreter, installed editable packages, child paths and migrations all came from that checkout. Two PostgreSQL 17.11 clusters and Redis ran in the same network namespace. The five original process cases passed together in 123.31 seconds. The separate delegation case passed in 7.21 seconds. The final API export on this tree had 98 origin examples for 98 registrations, and Alembic reported one head, `0022_quota_retry`. [Five process cases](raw/integrity-children-linux.xml) · [Workshop delegation case](raw/integrity-delegation-linux.xml) · [Migration and validator checks](raw/integrity-triage.json)

## Legacy capacity attribution

The frozen main push `434840593179e6c1faa6b7aeca3d7d747644506d` and the PR head `ce74f265b9072df52e9a485c6dfd48d5612d1907` ran the same P2, P3/P4 and P3 scale workflow configurations. Four of ten jobs failed on each SHA, on identical profiles. The matrix preserves the original gates and the observed variation between hosted runners. The 200-overload and open-arrival gaps have prior measurements in issue #13. [GitHub issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) · [Per-job links, returns and metric extracts](raw/hosted-capacity-comparison.json)

| Hosted profile | Frozen main | Candidate | Measurement |
|---|---:|---:|---|
| P2, 50 overload, two repeats | FAIL · 227.59 / 216.55 | FAIL · 120.23 / 101.25 | Actual first call starts/s, target at least 950 |
| P2, 200 overload, two repeats | FAIL · 36.19 / 35.23 | FAIL · 61.14 / 61.12 | Actual first call starts/s, target at least 950 |
| P3/P4, 200 overload | FAIL · 36.70 | FAIL · 35.43 | Actual first call starts/s, target at least 950 |
| P2, 50 and 200 mixed | PASS / PASS | PASS / PASS | Both repeats of both populations passed first-attempt capacity and bounded business recovery |
| P3/P4, 200 mixed and same-world CPU/SQL | PASS / PASS | PASS / PASS | Independent run and paired comparison |
| P3 scale, 500 mixed | PASS | PASS | First-attempt capacity and bounded business recovery |
| P3 scale, 500 fixed reads | PASS · 12,000/12,000 | PASS · 12,000/12,000 | Successful reads over 600 seconds. The separate long-duration gate was unexecuted in both jobs. |
| P3 scale, 1000 open | FAIL · 46.31 | FAIL · 42.43 | Observed HTTP header starts/s. Successful reads fell from 2037/5000 to 1815/5000, and transport errors rose from 3 to 26. |

The four failed profiles predate this branch. The 50-overload call-start rates and 1000-open successful-read rate were lower on this candidate measurement. The 200-overload P2 pair went in the opposite direction. These runs use separately assigned GitHub-hosted runners, so they do not identify a change responsible for either movement. Every 50/200 overload repeat on this pair reported correctness and bounded business recovery PASS. The 1000-open job counts first attempts only. [Selected hosted evidence](raw/hosted-capacity-comparison.json)
