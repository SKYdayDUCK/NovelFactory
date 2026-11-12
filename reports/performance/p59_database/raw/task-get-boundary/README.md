# Qualified task GET at 64 and 128 closed-loop clients

Preregistered source `0f48432a0d342de20ff0d996b9121384eaeb2da8`. The parent and sibling qualification jobs released the local machine first. The 64 block completed before 128 started. Each had its own fresh PostgreSQL cluster, and each of the four rounds had a fresh migrated database and object directory. The application and driving event loop run in one process. The broker uses two works, 16 bearer grants and four DB connections. These are client-coroutine counts. HTTPX 0.28.1 uses its default pool of up to 100 simultaneous connections, including at the 128-client setting.

|Clients|Mode in execution order|Verified / 512|429|Goodput/s|P95 ms|P99 ms|SQL calls|Peak connections|Client + server CPU ms/verified|
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
|64|baseline|512|0|129.17|1216.94|1922.51|2560|4|8.305|
|64|fenced|512|0|151.89|606.97|888.01|1536|4|7.304|
|64|fenced|512|0|133.20|1198.39|1754.29|1536|4|7.868|
|64|baseline|512|0|138.60|662.91|927.80|2560|4|8.148|
|128|baseline|512|0|131.85|2056.34|2756.42|2560|4|8.240|
|128|fenced|512|0|146.63|1816.38|2322.36|1536|4|7.334|
|128|fenced|512|0|147.56|2282.33|2630.76|1536|4|7.653|
|128|baseline|485|27|113.77|2415.45|2717.92|2452|4|9.796|

All 4,096 attempts remain in the raw samples and latency distributions. Every 200 response passed the entire authorized-body equality check. Connections returned to zero after every round. At 64 both orientations saved 40% of SQL executions, yet the reverse comparison raised P95 by 80.78% and P99 by 89.08%, exceeding the 10% allowance. Arithmetic mean per-round goodput was 133.89 baseline and 142.55 fenced. The original runner exited 0 because all operations completed; the broader tail-latency selection gate still failed.

At 128 the candidate verified 1,024/1,024 GETs across both rounds. Its paired goodput improved 11.21% and 29.70%, with P95 changes of −11.67%/−5.51% and P99 changes of −15.75%/−3.21%. Mean goodput was 122.81 baseline and 147.09 fenced. The reverse baseline reported all 27 errors as HTTP 429, yielding 997/1,024 verified baseline operations. The original runner exited 1. The observed candidate success applies to this tested point, with the reverse throughput difference affected by baseline rejection. The two blocks do not qualify a continuous range or a 950/s open-arrival target.

Each original JSON is gzip-compressed. Source SHA, environment, start/end host observations and block exit codes are in `environment-64.json` and `environment-128.json`. Request and round hashes are in `summary.json`, with unrounded round metrics in `rounds.csv`. No retries were issued after a failed block.

```sh
python -m benchmarks.p59_task_http --fenced --count 512 --concurrency 64 --output saturation-64.json
python -m benchmarks.p59_task_http --fenced --count 512 --concurrency 128 --output saturation-128.json
```
