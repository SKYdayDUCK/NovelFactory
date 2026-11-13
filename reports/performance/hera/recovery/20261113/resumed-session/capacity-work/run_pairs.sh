#!/usr/bin/env bash
set -euo pipefail
w=$(cd "$(dirname "$0")" && pwd)
case "$w" in */capacity-work) ;; *) exit 1;; esac
pglab=/workspace/scratch/d3dcbcf2244b/env-lab
export PATH="$pglab/venv/bin:$pglab/pgbin/usr/lib/postgresql/17/bin:$PATH"
export LD_LIBRARY_PATH="$pglab/pgbin/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
export PGHOST=127.0.0.1 PGUSER=agent PGDATABASE=postgres PGPORT="$(python -c 'import socket; s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
export NF_POOL_SIZE=8 NF_POOL_OVERFLOW=0 NF_BENCH_DIAGNOSTICS=1
export PYTHONDONTWRITEBYTECODE=1 P3_CLIENT_ROOT=/workspace/scratch/749fdd922bc0/baseline
export PYTHONPATH="$P3_CLIENT_ROOT/src:$P3_CLIENT_ROOT/workshop/src:$P3_CLIENT_ROOT"
cd "$P3_CLIENT_ROOT"
initdb -A trust --data-checksums -D "$w/db" -U agent -L "$pglab/pgbin/usr/share/postgresql/17" --locale=C --encoding=UTF8 > "$w/init.log"
pg_ctl -D "$w/db" -l "$w/postgres.log" -o "-h 127.0.0.1 -p $PGPORT -k ''" -w start
trap 'pg_ctl -D "$w/db" -m fast -w stop' EXIT
createdb nf_test
export NF_TEST_DATABASE_URL="postgresql+psycopg://agent@127.0.0.1:$PGPORT/nf_test" NF_DATABASE_URL="postgresql+psycopg://agent@127.0.0.1:$PGPORT/nf_test"
for agents in 50 200; do
 for arm in a1 b1 b2 a2; do
  export P3_ARM_DIR="$w/local-p2/$agents/$arm"
  mkdir -p "$P3_ARM_DIR"
  if [[ "$arm" == a* ]]; then export P3_API_ROOT="$P3_CLIENT_ROOT"; else export P3_API_ROOT="$w/candidate"; fi
  date -u "+%FT%TZ $agents $arm start"
  rc=0
  python "$w/frozen_p2_client.py" --agents "$agents" --profile overload --hot-fraction 0.8 --arrival-rps 10 --open-loop-rps 1000 --rounds 5 --max-attempts 5 --retry-budget-seconds 30 --gate-scope capacity --service-mode isolated-process --api-processes 1 --output "$P3_ARM_DIR/load.json" > "$P3_ARM_DIR/run.log" 2>&1 || rc=$?
  echo "$rc" > "$P3_ARM_DIR/original.exit"
  if [[ ! -s "$P3_ARM_DIR/load.json" ]]; then cat "$P3_ARM_DIR/run.log"; exit 1; fi
  date -u "+%FT%TZ $agents $arm complete rc=$rc"
 done
done
