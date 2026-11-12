# Reproduce the measurements

Run these commands from a checkout at the recorded SHA. Start from main `5e322e8af8ddb4465f68f8cab84c9685832461a9` for BASELINE. Use the product commit containing the single scope-upsert change (`5cfcfa4` on the P5.9 development line), or apply that change alone to the base, for the isolated candidate. Keep this evidence directory available separately if switching trees. A pinned uv environment, installed PG17, Redis server/CLI and a Linux host with loopback networking are required. Use a fresh output folder for each invocation.

```bash
uv sync --locked --group dev --python 3.12
export PATH="$PWD/.venv/bin:$PATH"
export PG_BIN=/usr/lib/postgresql/17/bin
export EVIDENCE="$(realpath reports/performance/p59_optimization)"
export OUT="$(mktemp -d)"
export PYTHONPATH="$PWD/src:$PWD:$PWD/tests/integration:$EVIDENCE/scripts"
python scripts/with_postgres.py --migrate --log-dir "$OUT/context-session" -- python -m benchmarks.context_profile --label BASELINE --entities 250 --history 3 --subjects 1 20 100 --repeats 5 --output "$OUT/context.json"
python scripts/with_postgres.py --migrate --log-dir "$OUT/write-session" -- python "$EVIDENCE/scripts/write_baseline.py" "$OUT/write.json"
P59_OUTPUT="$OUT/matrix" P59_SAMPLES="$OUT/matrix/samples.json" python scripts/with_postgres.py --migrate --log-dir "$OUT/matrix-session" -- bash "$EVIDENCE/scripts/redis_matrix.sh"
python scripts/with_postgres.py --migrate --log-dir "$OUT/providers-session" -- python "$EVIDENCE/scripts/multi_project.py" "$OUT/multi-project.json"
python scripts/with_postgres.py --migrate --log-dir "$OUT/fair-session" -- python "$EVIDENCE/scripts/tenant_fairness.py" "$OUT/fair.json"
P59_OUTPUT="$OUT/statements" bash "$EVIDENCE/scripts/statement_profile.sh"
python scripts/with_postgres.py --log-dir "$OUT/at30-session" -- python "$EVIDENCE/scripts/at30.py" "$OUT/capacity" 200-overload-1 200-overload-2 1000-open 500-soak 500-mixed-1 50-overload-1 50-overload-2 50-mixed-1 50-mixed-2 200-mixed-1 200-mixed-2 10-mixed-1
```

On the **candidate** checkout, execute the paired transaction harness and concurrent/rollback integration checks using the same environment. The pair script loads the exact baseline method from Git; retain the baseline commit object.

```bash
python scripts/with_postgres.py --migrate --log-dir "$OUT/pair-session" -- python "$EVIDENCE/scripts/scope_pair.py" "$OUT/pair.json"
python scripts/with_postgres.py --migrate --log-dir "$OUT/validation-session" -- python -m pytest -q tests/integration/test_scope_identity.py tests/integration/test_history_epoch.py tests/integration/test_derived_worker.py tests/queries
```

The local AT-30 launcher wraps each scenario's real exit status and never discards a failed result. Inspect **each status.json and capacity_gate**; the outer supervisor succeeds when it finishes collecting evidence. `gzip -dc` restores original JSON bytes. CPU affinities and resources used in each original run are stored per scenario. Calibration ran the original `benchmarks.client_calibration` client for two repetitions at the observed 2104-byte response size with one CPU per process.
