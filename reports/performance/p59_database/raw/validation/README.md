# Correctness and recovery evidence

The package validation output and the 35 independent mechanism tests use the repository commands below. Run them in a disposable checkout because the package checker regenerates its tracked JSON result.

```sh
uv sync --locked --group dev
uv run --no-sync python validation/validate_package.py
uv run --no-sync python -m unittest discover -s experiments -p 'test_*.py'
```

## Native PostgreSQL and Redis

Use a fresh PostgreSQL 17.11 cluster, a separate loopback-only Redis 8.0.2 instance, a fresh object directory and a Python 3.12 virtual environment for each run. Give the NF test database role permission to create the disposable fixture databases. Expose the PostgreSQL client and server tools and redis-server on PATH. Set `PG_BIN` to the PostgreSQL 17 binary directory. In the recorded Linux environment the database used fsync and synchronous commit, 128 MB shared buffers and a maximum of 60 connections.

The NF suite receives `NF_TEST_DATABASE_URL` and `NF_DATABASE_URL` in SQLAlchemy `postgresql+psycopg://` format. Point `NF_TEST_REDIS_URL` and `NF_REDIS_URL` at separate logical databases of the dedicated Redis service. The Workshop suite uses a separate database owned by a non-superuser and a plain `postgresql://` DSN in `AW_DATABASE_URL`. Set `AW_REDIS_TEST_URL` to its dedicated Redis logical database. No live model or user credential is needed.

```sh
uv run --no-sync python -m pytest -q tests --junitxml=nf.xml
uv sync --project workshop --locked --group dev --extra streams
(cd workshop && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m pytest -q tests --junitxml=workshop.xml)
uv run --no-sync python scripts/pitr_drill.py --with-redis --shared-token-budget --creative-harness --output pitr-out
```

`pitr.json` is the original physical PITR result including the selected recovery point, Redis delivery reconciliation, shared budget, count of retained UNKNOWN provider calls, frozen creative packet and world receipt. RTO refers to the fixture time up to a verified usable recovery.

## Workshop catalog check

The first Workshop run inherited the instrumentation extension in the public schema of template1. Four migration tests correctly found the extra catalog objects when comparing public to a fresh scoped schema. Compressed original JUnit and stdout have the `template-extension.gz` suffix. For the second complete run the template was pristine. PostgreSQL still preloaded the collector, and its views were installed only in the separate measurement and maintenance databases. All 120 runnable tests passed. The original Docker-sidecar stop/restart test requires `AW_REDIS_TEST_CONTAINER` and was skipped in both local runs. Preserve that distinction when interpreting `workshop.xml.gz`.

Each compressed file is original program output compressed with gzip. Use `gzip -dc FILE.gz` to inspect it. Traces use generated fixture paths and identities.

## Physical restart with a surviving coordinator

`pitr-568f949.json` repeats the PITR drill on an immutable detached checkout of `568f949d8068faa4b987a9dfcae0e2b8468a246e`. The separate `restart-568f949.json` covers an actual immediate PostgreSQL shutdown on the same product snapshot with the coordinator and connection pool kept alive. It tests warm reads, fail-closed read outcomes while the listener is down, WAL recovery of the same cluster, new reads and the original UNKNOWN reservation. The probe creates and stops only its own temporary cluster. It does not accept an existing database URL or data directory.

```sh
uv run --no-sync python -m benchmarks.p59_db_restart --pg-bin /usr/lib/postgresql/17/bin --output restart.json
```

## Development-tree smoke and fixed snapshots

`working-tree-regression.log.gz` and `working-tree-regression.xml.gz` retain the initial unfiltered NF run started during development. Pytest collected classes before all new tests existed, and subsequent subprocesses could load later edits in that writable tree. The run completed with 1,031 passing tests, 17 passing subtests, 4 skips and one failure. `working-tree-validator.json` identifies the failed assertion as a local link to then-unarchived raw Actions output in an in-progress candidate review. The application and database test cases in that run passed. The three Windows-specific checks and the dual-cluster S6 check were skipped by their existing platform or fixture markers.

The fixed `568f949` S6 test was run separately with two fresh PostgreSQL 17 clusters using independent system identifiers. Each fixture creates a distinct least-privilege role and database, and tests cross-service login denial before comparing the three offline scene conditions. The original test passed. `dual-cluster-s6.xml.gz` and `dual-cluster-comparison.json.gz` are its raw outputs. Set `AW_DUAL_TEST_ADMIN_URL` and `AW_DUAL_TEST_NF_ADMIN_URL` to the two independent disposable administrative DSNs, set `AW_NF_SOURCE` to the checkout, and run the following using the locked NF environment with the matching Workshop package installed.

```sh
NF_S6_COMPARISON_OUTPUT=comparison.json python -m pytest -q tests/integration/test_s6_engineering_comparison.py --junitxml=s6.xml
```

The final runtime revision `353106cf94b529450b6966a4b5d81b2b9f9c3be0` changed the coordinator's database-connection error to `SERVICE_UNAVAILABLE`, retryable with a `WAIT` recovery instruction. `restart-353106c.json` verifies three rejected reads during a real immediate shutdown and their production error serializer's 503 status and Retry-After header. The same live coordinator classified each read and resumed all three after WAL recovery. The idempotent reservation remained unique and the provider dispatch fence rejected a repeat send. `pitr-353106c.json` independently exercises physical base-backup recovery and Redis reconciliation on that revision.

The first fixed-snapshot complete run, `568f949-regression.log.gz` and `568f949-regression.xml.gz`, collected the unfiltered NF root suite and finished with 1,041 passed, 4 skipped and 17 passing subtests in 1,130.92 seconds. The dual-cluster S6 follow-up is recorded above. Three checks require native Windows.

The later `9a69d2c02cd6dedcfa8c90ac10ffecc2e2c25979` experiment adds the fenced singleton implementation and its integration test relative to `353106c`. A fresh detached checkout and private PostgreSQL ran `python -m pytest -q tests/integration/test_p59_fenced_status.py`: one passed. The original JUnit is `fenced-delta-9a69d2c.xml.gz`.
