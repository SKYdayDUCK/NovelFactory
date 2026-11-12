#!/usr/bin/env bash
set -eu
bindir="$(dirname "$(command -v redis-server)")"
tmp="$(mktemp -d)"
port="$(python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
redis-server --bind 127.0.0.1 --port "$port" --save '' --appendonly no --dir "$tmp" >"$tmp/redis.log" 2>&1 &
pid=$!
trap 'kill "$pid" || true; wait "$pid" || true; rm -rf "$tmp"' EXIT
export NF_TEST_REDIS_URL="redis://127.0.0.1:$port/15"
for i in {1..50}; do
    if "$bindir/redis-cli" -h 127.0.0.1 -p "$port" ping | grep -q PONG; then break; fi
    sleep .05
done
bash "$(dirname "$0")/matrix.sh"
