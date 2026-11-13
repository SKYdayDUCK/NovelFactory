# Execution budgets and durable supervision

Tasks, leases, attempts, roles, work and deadlines live in the existing NF PostgreSQL tables. All ingress paths must use current-grant locks. Database operations release their connections before a supervisor calls a model. The combined PostgreSQL supervisor can continue short control work with its local model executor fully occupied. Enable `scripts/run_supervisor.py --control-while-busy` to service committed wake hints and periodic reconciliation without claiming another model execution. Busy rescans have a 100 ms minimum separation. The Redis dispatcher already runs world, recovery, model and compute as individually configurable processors.

## Class ceilings

Migration `0024_hera_resources` extends the existing configuration table with two optional subjects under `RESOURCE`. No historical task or lease is rewritten. A MODEL lease comes from `nf_dispatch_slots`; CPU_COMPUTE uses `nf_index_leases`. Both class checks run under the same database advisory transaction lock as existing admissions, before a job can run. Both additionally honor the existing GLOBAL, WORK and ROLE limits. Changing a cap only affects new admissions, and a downscale lets active jobs drain. The preexisting CLI that materializes index outbox messages uses the same compute lease when any resource cap is installed. Its durable outbox ACK and replays remain intact.

For a two-slot installation wanting one model and one index working simultaneously, install these operator-owned bounds after upgrading the supervisor and standalone index worker. Each SQL change is atomic; active process-local pool sizes remain unchanged.

```sql
INSERT INTO nf_dispatch_limits(scope, subject, slots)
VALUES ('RESOURCE','MODEL',1), ('RESOURCE','CPU_COMPUTE',1)
ON CONFLICT(scope,subject) DO UPDATE SET slots=EXCLUDED.slots;
```

A zero class limit blocks new work of that kind. Connections, local executor threads and provider concurrency are distinct limits. Configure one aggregate PostgreSQL connection envelope across API processes, supervisors, listeners, Workshop and spawned index processes; a local `--db-pool-size` only caps its own parent pool. The benchmarked REST and MCP adapters share one zero-overflow engine. These changes do not supply a hard cross-process pool for arbitrary additional deployments. DB_READ_HEAVY/DB_WRITE/maintenance/notification have different existing execution entry points; general cross-process reservations for each of those classes remain unimplemented.

To roll back the runtime policy, remove the new process flag and only the RESOURCE rows installed for this deployment. Keep existing GLOBAL, WORK and ROLE rows, provider history, task executions and outbox/inbox history. Downgrading 0024 validates that no custom RESOURCE configuration remains. Earlier workers can ignore class caps, so activate them after all relevant workers have upgraded.
