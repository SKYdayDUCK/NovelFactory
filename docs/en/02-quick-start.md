# Run a six-scene volume

[简体中文](../zh-CN/02-快速开始.md) · [Project home](../../README.en.md)

Generate a short volume in a temporary PostgreSQL cluster, read the scenes, and trace the inputs and receipts for each scene. A deterministic model fixture supplies the text. You can run the entire example without a model account.

## Install the prerequisites

Use an ordinary Linux or WSL2 user account with Git, uv, and the PostgreSQL 17 server and client tools. Allow disk space for a Python environment, a temporary database and the generated book. Run the following commands to install the project's pinned Python 3.12.14 environment.

```bash
git clone https://github.com/SKYdayDUCK/NovelFactory.git
cd NovelFactory
uv python install 3.12.14
uv sync --locked --group dev
export PG_BIN="${PG_BIN:-/usr/lib/postgresql/17/bin}"
"$PG_BIN/postgres" --version
```

If your distribution uses another installation path, set `PG_BIN` to the directory containing `postgres`, `initdb`, `pg_isready` and `psql`. For a Docker-based installation, see [Configuration and deployment](08-configuration-and-deployment.md).

## Generate the book

From the repository root, create a fresh output directory. The wrapper picks a loopback port, initializes and migrates a disposable cluster, runs the writing process, and shuts the cluster down on exit. The book, object files and run logs remain in the directory you created.

```bash
export NF_DEMO="$(mktemp -d)"
.venv/bin/python scripts/with_postgres.py --migrate --log-dir "$NF_DEMO/postgres" -- \
  .venv/bin/python scripts/run_short_volume.py --execution-kind TEST_FIXTURE \
    --object-root "$NF_DEMO/objects" --output "$NF_DEMO/book"
```

| File | What to inspect |
| --- | --- |
| `book/short_volume.md` | All six scenes of the Chinese sample volume, 《潮闸余灯》 |
| `book/run_manifest.json` | Execution mode, scene and call counts, engineering result and author assessment status |
| `book/process_trace.jsonl` | The sequence of operations and their receipts |
| `book/initial_plan.json`, `book/final_plan.json` | The plans before and after writing |
| `book/world_receipts.json`, `book/contexts.json` | Accepted world changes and the material made available to tasks |

```bash
.venv/bin/python - "$NF_DEMO/book/run_manifest.json" <<'PY'
import json
import sys
from pathlib import Path

run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for field in ("engineering_status", "scene_count", "model_fixture_call_count",
              "real_model_call_count", "literary_quality_status"):
    print(field, run[field])
PY
```

The verified run reported `PASS`, six scenes, 15 fixture calls and zero real model calls. The accepted world event count was seven before and after writing. Literary assessment was `NOT_EVALUATED`. Open `short_volume.md` to read the book and `author_feedback.md` to record a reader assessment.

To register your own work, set up the Workshop database and notification service, create service identities, and configure model routing. [Configuration and deployment](08-configuration-and-deployment.md) · [Sample volume runner](../../scripts/run_short_volume.py)
