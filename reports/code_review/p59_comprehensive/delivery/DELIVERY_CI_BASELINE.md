# GitHub Actions control runs and P5.9 release comparison

Repository `SKYdayDUCK/NovelFactory`. Read-only GitHub observation at 2026-11-12 15:56:38 UTC. Each link below was returned by the Actions REST API.

## Effective branch configuration

The [main branch endpoint](https://api.github.com/repos/SKYdayDUCK/NovelFactory/branches/main) reported `5e322e8af8ddb4465f68f8cab84c9685832461a9`, `protected: false`, protection disabled, required status-check enforcement off and empty check/context lists. The [ruleset collection including parents](https://api.github.com/repos/SKYdayDUCK/NovelFactory/rulesets?includes_parents=true) returned an empty list. The administration-only protection endpoint returned 403 to the integration. The run recommendations below are engineering acceptance requirements, independent of GitHub branch enforcement.

## Original controls

### Frozen main after the documentation-only PR #31

Head `5e322e8af8ddb4465f68f8cab84c9685832461a9`.

| Workflow run | Job results |
| --- | --- |
| [P1 and P2 production integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43228784812) SUCCESS | [PostgreSQL 17 integration, concurrency and restore](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43228784812/job/131218902619) SUCCESS · [Schema, domain, HTTP and production boundaries](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43228784812/job/131218902822) SUCCESS |

### Comparable product and test PR #29

Head `60641790dabe34c3b9f68a7820c794f6287372d2`.

| Workflow run | Job results |
| --- | --- |
| [P5 native desktop resume](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704789) SUCCESS | [native-desktop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704789/job/131198000343) SUCCESS |
| [P6 offline experiment and authorization checks](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704887) SUCCESS | [offline](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704887/job/131198000982) SUCCESS |
| [P4 real isolated WAL PITR and verified recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704737) SUCCESS | [Physical base backup, SIGKILL, WAL replay and application verification](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704737/job/131198000789) SUCCESS |
| [Creative Harness engineering](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654) SUCCESS | [P5.7 pitr](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654/job/131198000337) SUCCESS · [P5.7 dual](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654/job/131198000664) SUCCESS · [P5.7 comparison](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654/job/131198000829) SUCCESS · [P5.7 harness](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654/job/131198000865) SUCCESS · [contracts-and-static-runtime](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704654/job/131198000909) SUCCESS |
| [P3 P4 measured evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704746) FAILURE | [Independent load process / 200 / overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704746/job/131198001367) FAILURE · [Same world CPU and SQL paired comparison](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704746/job/131198001787) SUCCESS · [Independent load process / 200 / mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704746/job/131198002255) SUCCESS |
| [Agent Workshop PostgreSQL and stdio](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704773) SUCCESS | [W6 message capacity and end-to-end scenes](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704773/job/131198000628) SUCCESS · [Matched admission and extended scene presets](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704773/job/131198001060) SUCCESS · [workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704773/job/131198001135) SUCCESS |
| [P2 measured capacity and recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876) FAILURE | [200 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001506) SUCCESS · [50 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001701) SUCCESS · [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001736) FAILURE · [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001881) FAILURE |
| [P3 scale experiments with strict unmet gates](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704724) FAILURE | [Measured 500-mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704724/job/131198001142) SUCCESS · [Measured 500-read-soak](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704724/job/131198001436) SUCCESS · [Measured 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704724/job/131198001691) FAILURE |
| [P1 and P2 production integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704846) SUCCESS | [Schema, domain, HTTP and production boundaries](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704846/job/131198000996) SUCCESS · [PostgreSQL 17 integration, concurrency and restore](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704846/job/131198001517) SUCCESS |

### Same product tree after PR #29 merged to main

Head `630c4bcf1f1a7d71f1632d6eb2d273923ac08014`.

| Workflow run | Job results |
| --- | --- |
| [Publish the reviewed P5.6 engineering tag](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43227003270) SUCCESS | [tag](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43227003270/job/131212639811) SUCCESS |
| [P1 and P2 production integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668528) SUCCESS | [PostgreSQL 17 integration, concurrency and restore](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668528/job/131211502453) SUCCESS · [Schema, domain, HTTP and production boundaries](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668528/job/131211503060) SUCCESS |
| [P4 real isolated WAL PITR and verified recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668533) SUCCESS | [Physical base backup, SIGKILL, WAL replay and application verification](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668533/job/131211501753) SUCCESS |
| [P6 offline experiment and authorization checks](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668645) SUCCESS | [offline](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668645/job/131211502007) SUCCESS |
| [Creative Harness engineering](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897) SUCCESS | [contracts-and-static-runtime](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897/job/131211503626) SUCCESS · [P5.7 harness](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897/job/131211503975) SUCCESS · [P5.7 comparison](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897/job/131211503992) SUCCESS · [P5.7 pitr](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897/job/131211504064) SUCCESS · [P5.7 dual](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668897/job/131211504273) SUCCESS |
| [Agent Workshop PostgreSQL and stdio](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668984) SUCCESS | [workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668984/job/131211504847) SUCCESS · [W6 message capacity and end-to-end scenes](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668984/job/131211505128) SUCCESS · [Matched admission and extended scene presets](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668984/job/131211505403) SUCCESS |
| [P3 scale experiments with strict unmet gates](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668635) FAILURE | [Measured 500-read-soak](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668635/job/131211503048) SUCCESS · [Measured 500-mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668635/job/131211503212) SUCCESS · [Measured 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668635/job/131211503488) FAILURE |
| [P3 P4 measured evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668505) FAILURE | [Same world CPU and SQL paired comparison](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668505/job/131211502437) SUCCESS · [Independent load process / 200 / mixed](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668505/job/131211502994) SUCCESS · [Independent load process / 200 / overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668505/job/131211503267) FAILURE |
| [P2 measured capacity and recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608) FAILURE | [200 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503194) SUCCESS · [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503280) FAILURE · [50 agents / mixed / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503526) SUCCESS · [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503545) FAILURE |

## New product tree trigger comparison

Compared the public-file diff `5e322e8af8dd..0b7c63f5d0b1` against the repository workflow definitions. Exact branch conditions and path patterns are in p59-workflow-match.json. P5.9 development-branch pushes have no matching push branch pattern. A PR targeting main and a subsequent main push both match six workflows.

| Workflow | Expected for P5.9 PR → main | Interpretation |
| --- | --- |
| `agent-workshop.yml` | Triggered | Engineering and dual service, including message capacity and scenes. Expect all jobs green. |
| `creative-harness.yml` | Triggered | Engineering, static contracts, S1–S6, dual service, comparison and PITR. Expect all jobs green. |
| `p1-integrity.yml` | Triggered | Unit/contract/HTTP boundaries, bilingual API drift, PG17 integration/concurrency/recovery. Expect both jobs green. |
| `p2-load.yml` | Triggered | Expect mixed jobs green. Historical 50 and 200 overload are separately judged at the original thresholds. |
| `p3-p4-evidence.yml` | Triggered | Expect paired context CPU/SQL and mixed load green. Historical 200 overload missed the offered-rate gate. |
| `p3-scale-experiments.yml` | Triggered | Historical 500 mixed and 500 read soak passed in both comparable controls. Historical 1000 open missed the wire-rate gate. A fresh soak failure still needs its own failure evidence. |
| `p4-recovery-drill.yml` | Outside direct PR triggers | Separate WAL recovery drill. The current products do not match its path filters. Creative Harness PITR has its own matched job. |
| `p5-native-desktop.yml` | Outside direct PR triggers | Native desktop is pull-request/path or manual, with no automatic main push. |
| `p56-engineering-tag.yml` | Outside direct PR triggers | Downstream workflow_run after Workshop or P6 main completion with its own preconditions; excluded from core gate counts. |
| `p58-branch-archive.yml` | Outside direct PR triggers | Manual or narrowly path-filtered main archive maintenance. |
| `p6-offline.yml` | Outside direct PR triggers | P6 offline authority suite is path filtered. It has a successful prior control. |

## Historical failed jobs classified from their logs

Recovery of the offered workload is distinct from sustaining the requested first-call start rate. P2/200 and P2/50 judge first call starts, while P3/1000 judges complete wire-start coverage and rate. Both full-tree controls showed exactly four red job cells. Each job returned a capacity result and failed the named benchmark step. The selected raw lines are in capacity-job-receipts.json. A new setup, dependency, assertion, upload or service failure requires individual diagnosis, even in the same named job.

| Event | Failed job | Original named rate |
| --- | --- | ---: |
| pull_request | [Independent load process / 200 / overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704746/job/131198001367) | 64.409/s |
| pull_request | [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001736) | 37.100/s |
| pull_request | [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704876/job/131198001881) | 176.604/s |
| pull_request | [Measured 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43222704724/job/131198001691) | 47.985/s |
| push | [Measured 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668635/job/131211503488) | 40.171/s |
| push | [Independent load process / 200 / overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668505/job/131211503267) | 45.979/s |
| push | [200 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503280) | 43.793/s |
| push | [50 agents / overload / full capacity gate](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43226668608/job/131211503545) | 125.093/s |
