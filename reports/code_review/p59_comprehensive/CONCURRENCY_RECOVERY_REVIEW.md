# Concurrent operations and recovery

The event outbox is durable in NF's database. eventhub.py separates claiming, Redis publication, inbox recording, acknowledgement and reclaim. A Redis entry alone cannot authorize a new world effect. The worker can resume from a pending delivery after disconnect. Lease expiry fences old task results. The model ledger keeps a potentially sent request UNKNOWN and retains its reservation until reconciliation. A route change fences new scheduling separately from the old Attempt.

Reviewed recovery.py restore target checks, archive manifests, object inventories and task refencing; online_backup.py captures immutable interpretation assets; Workshop has independent paired-restore evidence. Distinguish isolated PostgreSQL 17 PITR, skewed two-cluster recovery and a deployed site's restore objective. This review uses isolated disposable databases. Prior tested restore results are linked from reports/p6_readiness/ACCEPTANCE.md.
