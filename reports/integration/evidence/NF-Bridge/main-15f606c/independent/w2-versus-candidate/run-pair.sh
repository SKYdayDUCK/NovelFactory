#!/bin/bash
set -eu
base="$(dirname "$(dirname "$(dirname "$(realpath "$0")")")")"
out="$(dirname "$(realpath "$0")")"
export PG_BIN="$base/qualification-w2/pg17/usr/lib/postgresql/17/bin"
export PATH="$PG_BIN:$PATH"
unset NF_BENCH_DIAGNOSTICS
for arm in w2 nfbridge; do
  if [ "$arm" = w2 ]; then cd "$base/published-w2-capacity"; else cd "$base/published-bridge-audit"; fi
  dst="$out/$arm"
  mkdir -p "$dst"
  date -u +%FT%TZ > "$dst/start-utc.txt"
  cat /proc/loadavg > "$dst/start-load.txt"
  git rev-parse HEAD HEAD^{tree} > "$dst/source.txt"
  git status --porcelain > "$dst/dirty.txt"
  result=0
  .venv/bin/python scripts/with_postgres.py --migrate --log-dir "$dst/pg" -- "$PWD/.venv/bin/python" -m benchmarks.p3_open_arrival      --agents 1000 --rounds 5 --arrival-rps 1000 --hot-fraction .8 --api-processes 2      --output "$dst/result.json" > "$dst/benchmark.log" 2>&1 || result=$?
  echo "$result" > "$dst/exit.txt"
  date -u +%FT%TZ > "$dst/finish-utc.txt"
  cat /proc/loadavg > "$dst/finish-load.txt"
  test -s "$dst/result.json"
  tail -12 "$dst/benchmark.log"
done
