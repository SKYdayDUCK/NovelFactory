# Shared Token limit for a P6 experiment

Associate every participating task with the authorized experiment's single NF budget ID. The monetary budget, per-task Token limits, output cap and shared Token limit all apply. Prior to a model run, the authorized manifest's total token count must agree with the persisted budget. Changes to an existing ceiling require the recorded prior value and an authorized NF work-wide administrator. Each successful change writes an audited outbox event with the work, grant, former value and new value. A replay of the current value has no further effect.

```sh
python scripts/p6_budget.py --binding /srv/nf/private/project.json set --budget-id "$BUDGET_ID" --total-tokens 50000
python scripts/p6_budget.py --binding /srv/nf/private/project.json show --budget-id "$BUDGET_ID"
```

The numbers above are illustrative. Supply the amount and Token limits already authorized for the actual experiment. The private file follows the existing project binding format. Task bindings and branch-only administrators cannot inspect or change an entire work budget.

`total_tokens` is null for an existing uncapped budget. `reserved_tokens` includes PREPARED, SENT and UNKNOWN calls across all tasks and generations. `spent_tokens` includes actual usage on settled SUCCEEDED and FAILED attempts. `available_tokens` is the configured limit minus both quantities. New reservations against a capped budget require a positive Token reservation and enough available Tokens. Explicit settlement moves actual usage to spent and releases the difference. An unmatched external response retains its original attempt and the full reservation. If old records contain unsettled zero-Token reservations or settled calls with missing Token usage, resolve the actual usage before enabling an experiment limit.

Migration `0017_nf_workshop_delegation` → `0018_p6_shared_tokens` adds an optional policy table, a budget-key index, the usage query and an insert trigger on attempts. Existing attempts supply the usage directly, so the migration does not rewrite historical invoices or world data. Building the index scans the attempt table and acquires a PostgreSQL share lock. Schedule that deployment for a window that accommodates the size of the call ledger. The ordinary reservation path locks its budget row, and the trigger enforces the same limit for a previous application binary using that ledger. Verify installation with `alembic current` and `show`. An active configured limit blocks downgrade. A compatible application or a verified paired backup preserves both policy and spend.

## Inspect a recovered experiment

Keep both dispatchers stopped while applying the [NF and Workshop recovery procedure](workshop-delegation.md#两库恢复时的操作顺序). After the NF fence, run `show` against the restored project binding. Compare NF's original attempt ID, state and actual usage with Workshop's observations for the same operation. For an UNKNOWN attempt, the NF budget still reserves the full maximum and the Workshop observation carries no actual Token count. Keep the hold until the same attempt receives verifiable usage. Repeating its matching settlement leaves the balance unchanged.

Task cancellation settles an unsent PREPARED reservation in the current generation with zero usage. The task lock serializes cancellation with the right to send. An already SENT or UNKNOWN attempt, or a PREPARED attempt retained from a fenced generation, stays reserved until reconciled. Replaying an existing cancellation receipt also releases eligible reservations left by an older release.

A shared ceiling can be changed from its currently observed value. For example, given a separately authorized new limit, use:

```sh
python scripts/p6_budget.py --binding /srv/nf/private/project.json set \
  --budget-id "$BUDGET_ID" --expected-total-tokens "$PREVIOUS_LIMIT" \
  --total-tokens "$AUTHORIZED_LIMIT"
```

A stale prior value produces `IDEMPOTENCY_CONFLICT`. A limit below spent plus reserved usage produces `BUDGET_EXCEEDED`. Inspect the current account again before deciding whether to submit a revised change.
