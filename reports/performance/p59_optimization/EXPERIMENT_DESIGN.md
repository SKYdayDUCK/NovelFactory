# Controlled experiment design

BASELINE uses commit 5e322e8af8ddb4465f68f8cab84c9685832461a9. Preserve all retries, rejected requests and failing exit statuses. CANDIDATE changes the scope-upsert method in an isolated worktree. A positive result needs identical final scope maps and evidence of a useful timing or round-trip difference. VALIDATED additionally requires actual command receipts, repeatable rollback, concurrency, changeset integrity and the applicable regression tests. Local capacity results are reported independently.

1. Record SQL counts and end-to-end accept latency on a normal SET_RELATION through Kernel.accept.
2. Against the same PostgreSQL database, measure unchanged code and candidate with equal initial state in transactions rolled back after each sample. Alternate AB/BA order across repeats. Retain per-call wall time, current process/thread CPU, transaction state, returned map equality, SQL calls, and the source hashes.
3. Use empty/single/four/large sets, duplicates, insert/update conflict and reverse input order. Exercise two simultaneous transactions and cancel a transaction after updating multiple batches. Compare original version counters after rollback and committed versions after concurrency.
4. Measure complete guarded acceptance through both implementations, compare their immutable world material and receipt replay and preserve failures. Retain small-world regressions.
5. Run unmodified original AT-30 commands from p2-load.yml and p3-scale-experiments.yml with their business and physical offer-rate gates. Benchmark thresholds, retries and timing points stay frozen. See scripts/at30.py for replay arguments.
