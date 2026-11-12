# Experiment results

1. The fixed main PG17 profile recorded the normal compiler, state reads and guarded updates before any candidate change. [Baseline](BASELINE.md)
2. The isolated candidate received an array of unique scope identifiers. PostgreSQL sorts the rows in C collation before ON CONFLICT DO UPDATE, so concurrent calls use one code-point-ordered lock sequence. A single text array holds the selection; there is no per-key network round trip. The branch transaction and scope readback use their original implementation. [Controlled comparison](BEFORE_AFTER.md)
3. For all 74 measured pairs (24 whole precommit writes and 50 direct scope transactions), both arms produced identical scope maps or state/manifest/selection/frontier witnesses. The five- and 100-item whole-operation medians improved by 5.6% in this run. Individual trials include slower candidate samples. Every direct 4/64/256-key candidate sample beat its paired baseline. Full postcommit receipt replay and overlapping independent PG17 transactions passed. [Regression](REGRESSION.md)
4. All nine small project/provider combinations and all 18 instrumented scenarios passed on the configured final environment. Two earlier runs retained a Redis test-environment failure, and one premature single-poll observation in the project harness stopped on deferred admission. [Operation distributions](OPERATION_MATRIX.md)
5. AT-30 kept its old workload, time measurement, success checks, retry budget and capacities. The 200-agent overload's speculative throughput target remains unmet for the original implementation and isolated candidate. [Capacity runs](CAPACITY_RUNS.md)

6. [Full validation experiment](VALIDATION_EXPERIMENT.md). All 89 frozen source fixtures agreed. Early invalid input improved from 3.261 to 0.323 ms with first-error validation, but common valid end-to-end results varied by input size. The guarded native-copy alternative slowed both sizes. No product changes were selected.

| Stage | Source and evidence |
| --- | --- |
| BASELINE | Main `5e322e8af8ddb4465f68f8cab84c9685832461a9`, repository.py SHA-256 `594fab543243f8116beff1c68d0e9143e433ed4c538abfea6e04f0332e53d947`, raw/context-baseline.json and raw/write-baseline.json. |
| CANDIDATE | Isolated worktree on the same main plus the scope-method patch. repository.py SHA-256 `5735197b6587dd6f05fd115b7dd1136bba3833eddabc706fc61f3105cb63fc60`, matched 74-pair results in raw/scope-pair.json. |
| VALIDATED | The candidate file exactly matches product commit `5cfcfa4001042fd1611df31c82618eb2690832c2`; every scope/manifest witness matched, with 45 focused PG17 and 2 concurrency/rollback passes. See raw/scope-regression.xml and raw/concurrent.xml. |
