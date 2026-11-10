# 第三远程检查点实际 CI 证据索引

提交：`971fbef29eceb422d77271d11841bae1abdfd73f`。ZIP digest 逐项核对；原始 ZIP、解包文件及 job logs 均保留。

## 实际负载（包括所有已下载重复）

| Run / repeat | Agent / profile | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s（扣重复） | 首试 P95 / P99 ms | 实际 call/header start/s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 42857853046 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.0870 | 135.97/296.19 | N/A |
| 42857853046 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1151 | 113.25/160.65 | N/A |
| 42857853046 / run-1 | 50 / overload | 6/6 | 123 | NOT_MET | PASS | 12.6454 | 2646.08/3770.09 | 234.08/96.07 |
| 42857853046 / run-2 | 50 / overload | 6/6 | 118 | NOT_MET | PASS | 12.1597 | 2684.28/3380.23 | 232.37/124.74 |
| 42857853046 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9057 | 260.99/410.71 | N/A |
| 42857853046 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9056 | 265.11/404.91 | N/A |
| 42857853046 / run-1 | 200 / overload | 21/21 | 643 | NOT_MET | PASS | 10.2938 | 12226.65/19475.77 | 32.10/22.56 |
| 42857853046 / run-2 | 200 / overload | 21/21 | 649 | NOT_MET | PASS | 10.0844 | 14078.08/22744.55 | 30.33/22.89 |
| 42857853112 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9060 | 362.57/1025.43 | N/A |
| 42857853112 / single | 200 / overload | 21/21 | 568 | NOT_MET | PASS | 11.7512 | 11842.88/18025.64 | 52.39/33.20 |
| 42857838475 / single | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9101 | 231.87/401.31 | N/A |
| 42857838475 / single | 200 / overload | 21/21 | 639 | NOT_MET | PASS | 10.5837 | 14803.79/23266.53 | 39.19/25.74 |
| 42857838507 / run-1 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.0915 | 112.67/195.51 | N/A |
| 42857838507 / run-2 | 50 / mixed | 6/6 | 0 | PASS | PASS | 8.1189 | 112.97/157.55 | N/A |
| 42857838507 / run-1 | 50 / overload | 6/6 | 122 | NOT_MET | PASS | 12.2288 | 3839.62/4566.57 | 103.09/72.44 |
| 42857838507 / run-2 | 50 / overload | 6/6 | 116 | NOT_MET | PASS | 12.2491 | 3568.44/4723.45 | 98.86/79.47 |
| 42857838507 / run-1 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9162 | 168.46/234.74 | N/A |
| 42857838507 / run-2 | 200 / mixed | 21/21 | 0 | PASS | PASS | 7.9166 | 161.27/199.14 | N/A |
| 42857838507 / run-1 | 200 / overload | 21/21 | 663 | NOT_MET | PASS | 10.4031 | 11209.62/18562.34 | 32.63/23.28 |
| 42857838507 / run-2 | 200 / overload | 21/21 | 613 | NOT_MET | PASS | 10.3721 | 13509.07/23654.49 | 32.81/22.67 |

## JUnit 实际计数

- `42857838549/13782744716/extracted/unit.xml`: tests=276, failures=0, errors=0, skipped=0, time=7.115s
- `42857838549/13783648008/extracted/postgres-all.xml`: tests=472, failures=0, errors=0, skipped=0, time=331.300s
- `42857853036/13782878146/extracted/unit.xml`: tests=276, failures=0, errors=0, skipped=0, time=6.274s
- `42857853036/13785145029/extracted/postgres-all.xml`: tests=472, failures=0, errors=0, skipped=0, time=270.915s

## 证据边界

- All listed load repetitions are included; no best-run selection.
- This index describes the third frozen checkpoint only. Current local source may have moved.
- Service diagnostic timing is inclusive and overlapping, and instrumentation may affect latency/throughput.
- Local no-NF_BENCH_DIAGNOSTICS experiments are separate and are not synthesized into GitHub results.
- Connection-sample wait totals are sample-weighted observations, not elapsed time.
- Declared 1000 RPS remains NOT_MET whenever actual call starts or first attempts fail the frozen gate.
- Business recovery PASS and completed_logical_operations do not mean every overload read succeeded: retain retry_exhaustion counts.
- GitHub runners expose 4 CPUs but actual CPU models differ, including EPYC 7763/9V45/9V74 and Xeon 6973P-C/8573C; only same-job repetitions share the same runner allocation.
