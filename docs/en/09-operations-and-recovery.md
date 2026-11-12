# Operations and recovery

[简体中文](../zh-CN/09-运维与恢复.md) · [Configuration and deployment](08-configuration-and-deployment.md)

When a writing task stalls, identify its work, branch, task generation and Attempt ID. NF records tasks, budgets, calls and accepted world changes in PostgreSQL. Text and other artifacts live under `NF_OBJECT_ROOT`, indexed by content hash. Workshop records installations, delegations and budget holds in its own database. Redis carries notifications. Both services recover their business records from PostgreSQL.

## Locate a stalled operation

| Symptom | Action |
| --- | --- |
| A cross-volume node stays pending | Run `--pipeline-status` with the work's Supervisor binding. Inspect the upstream handoff, node state, and Attempt IDs for the current and previous task versions. |
| A worker has not woken after a Redis outage | Check EventHub `status` for subscription state and the time of the last success. Once Redis is back, run one `repair` pass and one `relay --once` pass. |
| A call was sent and the host lost its receipt | Keep the original Attempt and budget hold. Reconcile the provider receipt or invoice against the NF ledger. A sent call recorded as `UNKNOWN` needs reconciliation. |
| A reconnected role sees a different tool list | Inspect its installation, work, branch, identity, generation and revocation status. Call `tools/list` in the role's current session. |

Run these read-only checks as the service operator. Set `NF_ACCESS` to the private Supervisor binding for the work. EventHub reads `NF_DATABASE_URL` and `NF_REDIS_URL`.

```bash
.venv/bin/python scripts/run_supervisor.py --access-file "$NF_ACCESS" --pipeline-status
.venv/bin/python scripts/run_eventhub.py status
```

## Back up and restore the NF database

Use an operator account with access to the NF database and object store, and PostgreSQL client tools of the same major version. An online backup reads a consistent database snapshot and checks the object manifest and the materials used to interpret historical records. The snapshot includes the works held in that NF database. Archive the backup directory as a unit.

```bash
umask 077
export NF_RECOVERY_RUN="$(mktemp -d)"
.venv/bin/python scripts/backup.py create "$NF_RECOVERY_RUN/backup" --online \
  --report "$NF_RECOVERY_RUN/create.json"
.venv/bin/python scripts/backup.py verify "$NF_RECOVERY_RUN/backup" \
  --report "$NF_RECOVERY_RUN/verify.json"
```

Create an empty PostgreSQL 17 database and set `NF_RESTORE_DATABASE_URL` to its DSN. Keep the source configuration available and choose a fresh object directory. Restore checks the backup hashes, target version and empty target, imports the data, and compares the snapshot. It then fences in-flight tasks by changing their generation so old workers cannot commit.

```bash
.venv/bin/python scripts/backup.py restore "$NF_RECOVERY_RUN/backup" \
  --restore-object-root "$NF_RECOVERY_RUN/restored-objects" \
  --report "$NF_RECOVERY_RUN/restore.json"
```

Inspect `snapshot_equal_before_fencing` and `fenced_tasks`. If you receive `RESTORE_TARGET_NOT_EMPTY`, use a new empty database and object directory. [Backup CLI](../../scripts/backup.py) · [Restore and generation fencing](../../src/novel_factory/infrastructure/recovery.py)

## Exercise WAL replay and recover both services

The drill below creates its own source and recovery clusters. Set `PG_BIN` to the PostgreSQL 17 binary directory, put `redis-server` on `PATH`, and run as an ordinary user from the repository root. Check the target log sequence number (LSN), replay record, verification result and budget state in the receipt.

```bash
export NF_DRILL="$(mktemp -d)"
.venv/bin/python scripts/pitr_drill.py --with-redis --shared-token-budget \
  --creative-harness --output "$NF_DRILL/pitr"
```

The restored databases can reflect different stages of the same delegation. For example, Workshop may have reserved a budget and NF may already have recorded a task receipt. Reconcile both ledgers under the original operation ID before resuming dispatch.

1. Stop the schedulers and workers. Restore the databases and matching NF object snapshot. Keep the NF outbound fence in place and run Workshop's `recovery freeze` for each installation.
2. Use the installation-specific NF launcher and run `aw-reconcile --lookup-only` in batches. Process every page so each delegation receives a fresh remote observation for the current freeze epoch.
3. Export NF's installation snapshot and Workshop's verified ledger. Use `manage_workshop_binding.py reconcile` on NF to check the Workshop snapshot, and `recovery permit` on Workshop to verify the NF receipts. Preserve discrepancies, operation IDs and `UNKNOWN` holds while resolving the ledgers. Resume service once they agree.

The dual-service drills cover two databases in one physical cluster, two independent physical clusters, and rebuilding Redis notifications after data loss. The Workshop operations guide gives the installation, freeze, paginated lookup and resume commands. [Workshop operator guide](../../workshop/docs/operations.md) · [WAL drill](../../scripts/pitr_drill.py) · [Paired restore tests](../../workshop/integration/test_paired_restore.py)
