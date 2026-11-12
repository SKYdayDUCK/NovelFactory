# Service and database profiles

The context compiler's 100-subject instrumented call took 354.48 ms. Of that, 217.94 ms was spent under validate_contract, including 194.59 ms under the validator's reference/constraint traversal. SQL cursor calls and time are reported separately. Cumulative profiler times nest. Profiled timing includes instrumentation and must not be compared directly with the 131.92 ms uninstrumented median.

Seven ordinary commits of SET_RELATION touched four unique scope keys. KernelRepository.bump issued one INSERT ON CONFLICT per key, followed by a scope map query. The seven complete accepts used 60, 61, 61, 61, 61, 61 and 61 SQL operations. raw/write-baseline.json includes the actual EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) for the indexed scope map. These data motivate reducing command count within the branch write transaction. The read-heavy AT-30 workload and the four-scope write profile have different operation mixes.

The unmodified AT-30 benchmark records the client offer, wire-start times, transport outcomes, SQL/pool metrics, database wait samples and per-process CPU in its evidence. Local synthetic provider 503s are independent of local admission-control 429s. No remote rate-limit samples were collected.
