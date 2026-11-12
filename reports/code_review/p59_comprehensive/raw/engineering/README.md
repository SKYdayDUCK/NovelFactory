# Engineering regression evidence

Each `*-cases.json` file contains the case identifier, outcome and duration from one JUnit execution. Each also identifies its original XML by SHA-256. Case-level output omits fixture payloads, credentials and connection URLs. Unfiltered XML and physical backup data stay in the isolated experiment workspace. `gate-summary.json` records suite process status, duration, raw log SHA-256 and checked-out Git commit. Tests with identical names in different runs are repetitions and must be counted within their respective run.

The native services are PostgreSQL 17.11, Redis 8.0.2 and Codex CLI 0.167.0-alpha.4. The two service databases run in separate PG clusters, each owned by a distinct unprivileged login. The fixture provisioner creates disposable random secrets for those logins. Cross-service integration runs in local processes over MCP stdio; Docker-specific notification-container stop/restart is reported as skipped locally. The separate-service dual-identity SSH provisioning drill requires host access to create operating-system users and is exercised by the agent-workshop GitHub workflow.

The original Workshop historical suite obtained 19 passes and two failed setup calls due to absent archived Git objects. Importing the two pinned rehearsal commits restored both cases. The separate signed W4 upgrade also passed. Sources were retained with their historical object IDs and the upgrade assertions stayed unchanged. Use the agent-workshop workflow's W6 step for the two source objects.

The original core run used temporary PostgreSQL without a Redis service. Dedicated reruns use Redis and the native host. The combined candidate run enables both from the start. A live model provider, commercial charges and reader evaluation need separate authorization and evidence.


## Reproduce

Use Python 3.12, PostgreSQL 17.11 and Redis 8.0.2. Point `PG_BIN` at the directory containing `postgres`, `initdb`, `pg_basebackup`, `pg_dump` and the PostgreSQL command-line clients. Put `redis-cli`, `redis-server` and the tested Codex CLI on `PATH`. Each invocation of `with_postgres.py` creates a disposable cluster.

```bash
uv sync --locked --group dev
uv sync --project workshop --locked --group dev --extra streams
uv pip install --python .venv/bin/python --no-deps ./workshop
.venv/bin/python scripts/with_postgres.py --migrate -- .venv/bin/python -m pytest tests -q
.venv/bin/python scripts/with_postgres.py -- .venv/bin/python scripts/clean_install_check.py --verify-postgres
.venv/bin/python scripts/P6_preflight_smoke.py --nf-checkout .
```

For all coverage in one top-level run, follow the PostgreSQL/Redis service setup in `.github/workflows/creative-harness.yml`, set both `AW_DUAL_TEST_*ADMIN_URL` variables and `AW_NF_SOURCE` to the source checkout, and run the same pytest command. Root service tests also need `NF_DATABASE_URL`, `NF_TEST_DATABASE_URL` and `NF_TEST_REDIS_URL`. The `workshop` job in `.github/workflows/agent-workshop.yml` declares service logins, migration database names, prior release Git objects and all historical and dual-service acceptance commands. The `p4-recovery-drill.yml` and `creative-harness.yml` workflows exercise physical recovery with full WAL replay and Redis reconstruction.

```bash
.venv/bin/python scripts/with_postgres.py --migrate -- .venv/bin/python scripts/run_short_volume.py \
  --execution-kind TEST_FIXTURE --output /tmp/nf-six-scenes --object-root /tmp/nf-six-scene-objects
.venv/bin/python scripts/pitr_drill.py --with-redis --shared-token-budget --creative-harness \
  --output /tmp/nf-pitr-check
```

Use new output directories for repeated runs. The measured short-volume walkthrough returned six scenes, 2,204 code points, 15 fixture turns, no real inference and seven accepted world events both before and after writing.
