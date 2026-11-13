# Durable task admission

Enable the additive database policy after deploying the HERA application and Alembic revision 0023 or later on every writer. Activate with a short operator transaction in the target installation.

```sql
UPDATE nf_durable_admission_policy
SET total_limit=10000, work_limit=5000, principal_limit=1000, resource_limit=5000,
    max_queue_seconds=3600, max_age_seconds=86400, enabled=true
WHERE singleton;
```

The policy is disabled initially. Existing task rows, generations, command receipts, outbox, supervisor jobs, leases and model attempts retain their original semantics. Queue admission reserves space for a distinct nonterminal task. Controllers authorize supervisor enrollment and an active provider route separately. The execution layer reserves a permit and budget before a model call. An authorized external worker may claim its task using its usual grant.

Inside the existing create transaction the kernel locks and checks the live authorization and frozen TaskSpec, acquires the command idempotency lock, and returns any matching receipt. A new command reads the policy and, when enabled, locks its row before reading authoritative occupancy and inserting the task. The same transaction writes its receipt, initial input, queue deadline and change event. HTTP can return 202 after this transaction commits. Conflicts and database errors roll the transaction back. A unique primary key fences task ID collisions across different command keys.

All nonterminal tasks (including running tasks, review waits and tasks created before activation) count toward global, work, principal and resource limits. The authenticated grant fixes the role. A server-managed role-to-class map determines its resource class, with MODEL as default. New admissions freeze that class and legacy tasks use the current map. Classification grants no execution or spending authority. Reducing capacity preserves all previously recorded tasks and receipts.

The requested wall deadline must fit the configured maximum age. The first-start deadline is the earlier of that wall deadline and the maximum first-start wait measured from admission. Both appear in the receipt. The queue selector excludes expired never-claimed tasks. A scoped recovery worker closes them and records an expiration event. Lease recovery and UNKNOWN model attempts retain their prior reconciliation rules. Run the authorized supervisor/reconciliation worker for each active project. Stopping all workers stops execution.

Rollback for new admissions is `UPDATE nf_durable_admission_policy SET enabled=false WHERE singleton`. Keep HERA-capable workers active until waits with first-start deadlines have drained. Previously accepted tasks remain accessible through their task APIs. Returning to the original write policy does not require a migration. A schema downgrade removes the optional policy and metadata after checking outstanding first-start waits. Task rows, commands and receipts survive.
