# Workshop acceptance runs

Use disposable PostgreSQL 17 databases and a local Redis instance. The NovelFactory and Workshop checkouts need their locked Python 3.12 dependencies in the same interpreter, including the Workshop streams extra. Put the NovelFactory checkout's pinned Codex CLI and Linux host tools on PATH. The narrative and budget fixtures use a loopback HTTP provider with zero external model calls.

## Dual-service scene

Set `AW_NF_SOURCE` to the NovelFactory checkout that contains the delegation adapter and the banquet prerequisite fixture. Set `AW_DUAL_TEST_ADMIN_URL` to the SQLAlchemy psycopg URL for a disposable PostgreSQL administrator able to create roles and databases. Set `AW_ACCEPTANCE_OUTPUT` to a new directory. From the Workshop project directory run:

```sh
export PYTHONPATH="$PWD/src:$AW_NF_SOURCE/src"
python -m pytest -q benchmarks/cross_service_scene.py --junitxml="$AW_ACCEPTANCE_OUTPUT/scenes.xml"
```

The fixture creates two distinct database owners, revokes PUBLIC CONNECT, verifies cross-database login rejection and destroys both databases after each module. The operator credentials provision the test databases. Both service processes receive their own private bindings. The two scenarios exercise Minimal and Standard staff, message delivery, actor prerequisites, an accepted world change, causal plan revision, a late stale read and an unaffected read, prose reconciliation, independent Workshop review, author retrieval and cost settlement. The S9 escalation has a review receipt. The short prepared excerpt and the programmed reviewer have no literary rating.

Each successful scenario writes a JSON record. Its source field fingerprints the implementation and fixtures loaded by that run. The Git head is the local checkout head. Compare build trees or content hashes when a remote publisher constructs an equivalent Git object with another commit identity.

## Capacity

`capacity-contract.json` sets the message load and `delegation-contract.json` sets the separate delegation load. For message tests, create three empty Workshop databases named `aw_benchmark_10`, `aw_benchmark_50`, and `aw_benchmark_200` with a dedicated Workshop owner. Set `AW_BENCH_DB_BASE` to that account’s PostgreSQL connection URI through the port (without a trailing slash or database name), and `AW_REDIS_URL` to disposable Redis. Use a fresh output file for each tier.

```sh
for members in 10 50 200
do
  export AW_DATABASE_URL="$AW_BENCH_DB_BASE/aw_benchmark_$members"
  python -m alembic upgrade head || exit 1
  python benchmarks/message_capacity.py --employees "$members" --output "$AW_ACCEPTANCE_OUTPUT/messages-$members.json" || exit 1
done
```

Message timings cover authenticated dispatcher calls and real SQL transactions. The dual-service scene separately exercises real stdio transport. To measure end-to-end delegation backlogs, use the two-checkout environment from the scene run and set `AW_BENCH_RESULTS` to a fresh directory.

```sh
python -m pytest -q benchmarks/delegation_capacity.py --junitxml="$AW_BENCH_RESULTS/delegations.xml"
```
