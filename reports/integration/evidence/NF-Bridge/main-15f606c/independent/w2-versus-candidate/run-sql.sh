#!/bin/bash
set -eu
out="$(dirname "$(realpath "$0")")"
base="$(dirname "$(dirname "$out")")"
export PG_BIN="$base/qualification-w2/pg17/usr/lib/postgresql/17/bin"
export PATH="$PG_BIN:$PATH"
for arm in w2 nfbridge; do
  if [ "$arm" = w2 ]; then cd "$base/published-w2-capacity"; else cd "$base/published-bridge-audit"; fi
  export PYTHONPATH="$PWD"
  dst="$out/$arm"
  .venv/bin/python scripts/with_postgres.py --migrate --log-dir "$dst/diagnostic-pg" -- "$PWD/.venv/bin/python"     "$out/request-sql.py" "$dst/query-trace.json" > "$dst/diagnostic.log" 2>&1
done
