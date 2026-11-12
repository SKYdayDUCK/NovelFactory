# Configuration and deployment

[简体中文](../zh-CN/08-配置与部署.md) · [Quick start](02-quick-start.md)

For core development, the [root Compose file](../../docker-compose.yml) starts a PostgreSQL test database on loopback port `55432`. For Workshop, per-work model routing, or paired restore, use the [two-service template](../../reports/p6_readiness/templates/wsl/compose.yaml).

## Native Windows and Linux runtime

Run the service as an ordinary user with Python 3.12 and uv, from the checkout root. The commands below use Windows PowerShell, a local NTFS object store and the disposable PostgreSQL 17 database from the root Compose file. Docker Desktop hosts the database. Python, MCP, HTTP and the supervisor run directly on Windows. Use a dedicated service role and credentials for a persistent installation.

```powershell
uv sync --locked --group dev --python 3.12
docker compose up -d --wait postgres
$env:PYTHONUTF8 = '1'
$env:NF_DATABASE_URL = 'postgresql+psycopg://nf_dev:nf_dev_local_only@127.0.0.1:55432/nf_test'
$env:NF_OBJECT_ROOT = Join-Path $env:LOCALAPPDATA 'NovelFactory/objects'
$private = Join-Path $env:LOCALAPPDATA 'NovelFactory/book-one'
uv run --no-sync python scripts/nf.py doctor
uv run --no-sync python scripts/nf.py migrate upgrade head
$book = uv run --no-sync python scripts/nf.py init-book --book reports/p6_readiness/templates/book.json --private-dir $private | ConvertFrom-Json
uv run --no-sync python scripts/nf.py grant-resume --admin-binding "$private/control.json" --work-id $book.world_ref.work_id --branch-id $book.world_ref.branch_id --object-root $env:NF_OBJECT_ROOT --hours 24 --output "$private/resume.json"
'{}' | uv run --no-sync python scripts/nf.py call --binding "$private/resume.json" nf_project_status
uv run --no-sync python scripts/nf.py serve --host 127.0.0.1 --port 8000
```

The bootstrap creates a new private directory with an owner-only inheritable ACL. An existing shared directory or an unverifiable binding fails closed. Put object storage under a service-owned local path. On Linux create the private directory with mode 0700; binding files use mode 0600. Both wheels carry the same private-file checks. Windows host cleanup uses a Job Object to contain child processes across cancellation and parent exit. Native Codex execution uses `codex.exe` on PATH or an absolute executable path in `NF_CODEX_EXECUTABLE`.

The administrator binding in `control.json` authorizes issuance. The branch-limited binding in `resume.json` performs status and resume operations. `grant-resume` creates its output exclusively. Reuse the existing unexpired binding or choose a fresh output filename for renewal. The example's 24-hour grant expires even if its file stays on disk. The `doctor` command checks native dependencies and reports configuration presence. The migration and authenticated status commands exercise the real database. Creating the book and checking its status make zero model calls.

The HTTP command runs in the foreground. To run a world processor in another terminal, set the same database URL, object root and private directory there. Ctrl+C starts graceful shutdown. A model processor additionally requires authorized runtime profiles and budget.

```powershell
uv run --no-sync python scripts/nf.py supervisor --access-file "$private/supervisor.json" --processor world --adaptive
```

The operator can restore eligible work through the branch binding using `'{}' | uv run --no-sync python scripts/nf.py call --binding "$private/resume.json" nf_resume_project`. Linux runs the same Python commands after setting the environment below. The original two-service bootstrap scripts and the Bubblewrap role-tool sandbox require Linux. The Windows job covers native bootstrap, real MCP stdio, simulated providers, object publication, lease recovery, cancellation and idle session retirement on real PostgreSQL 17. The job result identifies the exact tested commit.

## Provision the two-service installation on Linux

Start in the repository root on Linux or WSL2 with uv, Python 3.12, a running Docker Engine and Compose v2. Install each project's locked dependencies. Workshop's `streams` extra provides the Redis client for notifications.

```bash
uv sync --locked --group dev
uv sync --project workshop --locked --group dev --extra streams
umask 077
export P6_PRIVATE="$HOME/novelfactory-local"
export P6_SECRETS="$P6_PRIVATE/secrets"
mkdir -p "$P6_PRIVATE"
.venv/bin/python reports/p6_readiness/templates/wsl/make-secrets.py
docker compose -f reports/p6_readiness/templates/wsl/compose.yaml config --quiet
docker compose -f reports/p6_readiness/templates/wsl/compose.yaml up -d --wait
```

Run the secret generator when creating the databases. It generates separate passwords for both databases and Redis, and requires an owner-controlled secret directory with mode `0700`. On restart, export `P6_PRIVATE` and `P6_SECRETS` again and reuse the existing secrets and volumes. The template binds the service ports to loopback.

| Service | Port | Identity or use |
| --- | --- | --- |
| NF PostgreSQL 17.11 | `56431` | `nf_service` owns the `novel_factory` database |
| Workshop PostgreSQL 17.11 | `56432` | `aw_service` owns the `agent_workshop` database |
| Redis 8.0.2 | `56379` | Logical database 0 for NF and 1 for Workshop |

## Apply the migrations

These commands run in a local operator shell. For separate service processes, give each OS account its own database and Redis URLs. A role's workspace receives its binding and MCP launcher. The execution host reads the model credentials.

```bash
export NF_DATABASE_URL="postgresql+psycopg://nf_service:$(cat "$P6_SECRETS/nf_password")@127.0.0.1:56431/novel_factory"
export AW_DATABASE_URL="postgresql://aw_service:$(cat "$P6_SECRETS/aw_password")@127.0.0.1:56432/agent_workshop"
export NF_REDIS_URL="redis://:$(cat "$P6_SECRETS/redis_password")@127.0.0.1:56379/0"
export AW_REDIS_URL="redis://:$(cat "$P6_SECRETS/redis_password")@127.0.0.1:56379/1"
export NF_OBJECT_ROOT="$P6_PRIVATE/nf-objects"
.venv/bin/python -m alembic upgrade head
workshop/.venv/bin/aw-migrate upgrade
.venv/bin/python -m alembic current
workshop/.venv/bin/aw-migrate current
```

Each service account owns its database. Migrate empty databases for a new installation. When upgrading an existing installation, exercise the migration and resume in-flight tasks on a restored copy first. [Operations and recovery](09-operations-and-recovery.md)

## Bind an organization, a work and a host

1. An operator installs Workshop, creates an organization and issues the manager's private binding. The manager can publish an authorized routing policy for a work.
2. Set `NF_RUNTIME_PROFILES` on the NF execution service to its private provider configuration. Each `secret_ref` names a host environment variable. Replace the template's `model` placeholders with model names available to the account. A zero-budget registration supports status checks and route previews before authorizing inference.
3. An NF administrator creates a work, branch, budget and Supervisor binding for each book. Activate its routing policy, then register the pipeline. Inspect role routing with `runtime_policy.py preview` and registered nodes with `run_supervisor.py --pipeline-status`.

The two-book procedure includes the installation, registration and route activation commands. [Two-book operator walkthrough](../../reports/p6_readiness/README.md) · [Provider template](../../reports/p6_readiness/templates/providers.json) · [API reference](07-api-reference.md)

The engineering run exercised locked Python installs, two independent PostgreSQL clusters, Redis Streams and MCP stdio. Docker Compose v2.32.4 parsed both Compose files. Docker Desktop startup on Windows and billable provider access require verification on the target installation.
