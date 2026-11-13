# HTTP batch validation candidate

**Decision: rejected for production.** The existing HTTP batch route validates the entire envelope before authentication and the application validates it again. The experimental change reused the immutable, prepared request mechanism already in service for single queries. Direct callers still received schema validation. Custom query services still received mutable dictionaries. Schema errors retained precedence over missing authentication, and revoked credentials and frozen role cuts remained enforced. The focused real PostgreSQL HTTP, bound-tool and time-axis suite passed 23 cases. [Contract tests](contract-tests.xml)

The first trial ran baseline, candidate, candidate and baseline in separate processes on the same isolated PG17 cluster. Each process used a fresh live BELIEF task. A sequential loopback HTTP client checked every item against the authorized fixture payload. Outputs include all timed calls after warm-up. With 16 items the four median latencies were 153.17, 163.91, 179.13 and 183.12 ms. Between-run variability was larger than the validation cost. [Baseline 1](base-1.json) · [Candidate 1](candidate-1.json) · [Candidate 2](candidate-2.json) · [Baseline 2](base-2.json)

A second trial used the same active task, two warmed HTTP listeners, one PG17 database, one Python process and alternating call order. The two arms used the stock prepared candidate and a dictionary-service subclass that exercised both complete envelope checks. Both served identical results. Each row below is the median over 30 calls. CPU counts the local load client and Python servers together. PG runs as a separate process. [Full paired samples](paired.json)

| Items | Envelope checks, current → candidate | Time inside envelope validation (ms) | Client-to-response time (ms) | Python process CPU (ms) |
| --- | --- | --- | --- | --- |
| 1 | 2 → 1 | 2.19 → 1.09 | 68.05 → 67.05 | 18.63 → 18.16 |
| 16 | 2 → 1 | 11.63 → 5.71 | 136.14 → 142.09 | 86.34 → 84.15 |

Within the 30 individual 16-item pairs, only 13 candidate calls finished sooner and 14 used less process CPU. The candidate saved measurable schema work, but the endpoint did not show a repeatable improvement. Production continues to use the existing service and checks for batch calls.

To reproduce the paired candidate, use a disposable worktree and apply [prepared-batch.patch](prepared-batch.patch). With the project installed and the checkout on `PYTHONPATH`, run the following against a new cluster. Reuse the script without `--paired` on the frozen and patched sources for the separate-process experiment. The older separate-process output predates the addition of per-call CPU measurement.

```sh
git apply reports/performance/p59_runtime/http-batch/prepared-batch.patch
python scripts/with_postgres.py --migrate -- python reports/performance/p59_runtime/http-batch/experiment.py --paired /tmp/nf-http-batch.json
```
