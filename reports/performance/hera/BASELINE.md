# HERA preregistration

Frozen before implementation, on main bcb03637d4f01030fc95b99d1ff743b3758297ad. Database reference 9364b154941bfbbe95a57fee5ceb600f5219fa5b and MCP reference c4d4a76d2171fc0929e36c70d53bc65456912442. Use candidate patches individually. P6 #35 and Scion #4 are independent. Main CI has two failed historical performance gates. Retain AT-30 at 950 correct operations/s.

The reference 1000 SDK client read burst served 5 correct responses, 976 HTTP 429s, 7 tool backpressure and 12 transport errors. Host queueing served 1000/1000 in 35.26s; 860 completed within 30s. These are bound role reads. Compare completion times only for matching request contents, population, identity and fixed resource budgets. Database task admission, old role reads and novel completion have separate denominators.

## Predeclared gates

Use a single shared application engine, fixed pool cap, zero overflow, isolated fresh PostgreSQL 17. Measure SDK and REST concurrently with a common start barrier. Record process/session counts and effective in-flight concurrency separately from logical tasks. Keep errors in the offered denominator. A/B/B/A on identical data and budgets for default adoption: each reciprocal comparison must improve correct throughput and preserve correctness, with P95/P99 regression at most 20%. Report latency from scheduled start, first request/response, durable commit receipt and terminal completion separately. Strong authorization and idempotency are mandatory. Keep new optimizations opt-in pending independent acceptance.

| Target | Metric |
|---|---|
| Fast reads | At least 3x correct throughput versus full read, P95 below 100 ms |
| Burst | All 1000 legitimate commands durably acknowledged and final effects verified; matched read burst drains below 35.26s |
| Isolation | At most 20% fast-read degradation under heavy work |
| Resource cost | DB peak, SQL/request, CPU/RSS, queue age, pool usage, fairness by work |
| Failures | No duplicate effect, unauthorized reads, lost accepted tasks or double charges; recovery time measured |

Required matrix: 50, 200, 500, 1000, 2000, 5000 logical agents; transport mixes MCP percentages 0/20/50/80/100, distinguishing stdio and remote. For a transport sweep use one operation per logical client per phase; report bounded live clients explicitly. Include read burst, concurrent submission, shared-command retries, query under model backlog, Workshop wakeup, multi-work fairness, recovery. Preserve per-request raw evidence, report which points were actually executed. For any simulation record the provider as simulated. For algorithm candidates retain old failed conditions (64 concurrent HTTP read tail, global microbatch, DRR/WFQ and routing). No performance claims from SQL microbenchmarks alone.

Prior code and evidence: [database](https://github.com/SKYdayDUCK/NovelFactory/tree/9364b154941bfbbe95a57fee5ceb600f5219fa5b/reports/performance/p59_database), [MCP](https://github.com/SKYdayDUCK/NovelFactory/tree/c4d4a76d2171fc0929e36c70d53bc65456912442/reports/performance/p59_mcp), [runtime](../p59_runtime).
