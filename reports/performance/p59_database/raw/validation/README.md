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
