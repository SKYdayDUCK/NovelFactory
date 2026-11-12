#!/usr/bin/env bash
# Run from a frozen checkout with PG_BIN, Python environment and P59_OUTPUT set.
set -eu
: "${PG_BIN:?point at PostgreSQL 17 binaries}"
: "${P59_OUTPUT:?choose an output directory}"
test "$("$PG_BIN/postgres" --version | cut -d' ' -f3 | cut -d. -f1)" = 17
root="$(mktemp -d)"
port="$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
umask 077
"$PG_BIN/initdb" -D "$root/data" -U nf_dev --no-instructions --locale=C --encoding=UTF8 --data-checksums --auth-host=trust --auth-local=reject
"$PG_BIN/pg_ctl" -D "$root/data" -l "$root/server.log" -o "-h 127.0.0.1 -p $port -c unix_socket_directories= -c shared_preload_libraries=pg_stat_statements -c shared_buffers=128MB -c max_connections=60" start -w
trap '"$PG_BIN/pg_ctl" -D "$root/data" stop -m fast -w; rm -rf "$root"' EXIT
export PGHOST=127.0.0.1 PGPORT="$port" PGUSER=nf_dev PGDATABASE=p59
"$PG_BIN/createdb" p59
"$PG_BIN/psql" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION pg_stat_statements"
export NF_DATABASE_URL="postgresql+psycopg://nf_dev@127.0.0.1:$port/p59"
export NF_TEST_DATABASE_URL="$NF_DATABASE_URL"
python -m alembic upgrade head
mkdir -p "$P59_OUTPUT"
python "$(dirname "$0")/statement_profile.py" "$P59_OUTPUT"
