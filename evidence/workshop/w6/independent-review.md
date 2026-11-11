# Independent integration review

Reviewed the organization → restricted NF submission → settlement path, manager worker, audience-filtered history, sender-origin revocation, frozen skill provenance, review assignments and two-preset scene fixture. Reviewed sources include Workshop through w007 in the integration worktree and NF's delegation service in the bridge checkout. The author of W1/W2/W4 also reviewed those components; checks of W3, NF's bridge and W5 were an independent second pass. The P6 literary reviewers remain separate.

| Scenario | Evidence | Result and boundaries |
|---|---|---|
| Typed organization message, lost notification and double send/ACK | message-rerun/messages-{10,50,200}.json; tests/test_notifications.py | PASS. Re-ran the original contract on w007, including sender binding fences. 10/50/200 employees completed 30/150/600 exchanges. At 200, P99 send 75.41 ms and ACK 44.53 ms. Removing the dedicated stream and restoring 600 distinct hints took 5.06 s. Frozen contract SHA-256 is 76286b924ec0c4fbc20032d1c12d8c6e329735900100d5b58e7f7ff6af2221d5. This measures authenticated dispatcher/SQL paths and Redis recovery. Per-file hashes and the checkout HEAD are in each raw record. |
| Revoked sender and expired route | tests/test_preset_fences.py; tests/test_messaging.py | PASS. Old sent rows remain durable. Old body access, acknowledgments and in-flight assignments are denied when origin credentials or route grants are revoked. Real PG w002 → w006 preserves old mail with unknown origin and fences its active use. |
| NF policy boundary | application/delegation.py and interfaces/delegation_stdio.py in NF; integration/test_dual_stdio.py | NF authenticates installation and checks the work, branch, policy, slot-specific principal, role, actor, grant epoch, template, budget and original operation. One transaction creates the task, supervisor job and remote receipt. The returned fields reference task, world and accounting status. NF actor beliefs, private context text, prompts and model credentials do not appear in the receipt. The two-service integration uses separate owners/databases and enforces the incoming bridge via actual stdio. |
| Revocation after a model request was sent | tests/test_delegation.py; budgeted-review tests | Before sending, stale tenures release an unsent reservation. After NF acceptance, original-key lookups can observe and settle the same attempt. UNKNOWN retains its hold. The test uses real SQL and seeded or synthetic peer observations; it does not demonstrate provider invoice accuracy. |
| Authoritative review materials and independence | tests/test_reviews_skills.py; independent-regression.xml | Fixed an assignment path with no reviewer-facing source retrieval. The assigned current reviewer can read frozen source/evidence references. Other reviewers and actors are denied. Management receives the application state without the references through this tool. The intake requires an authenticated, trusted operator to verify the NF receipt and producer/coauthor/proxy group mappings. |
| Routine-vs-high-impact review | test_caller_cannot_downgrade_sensitive_source_to_routine_rule_pass | Fixed a caller-controlled downgrade. A deterministic rule PASS now bypasses independent review only when the frozen source is explicitly eligible for routine treatment and the request is sampled. Unmarked/legacy sources fail closed. Numeric rules execute locally. No literary score is inferred. |
| Reviewer departure and deadlines | test_review_get_reassignment_and_offline_expiry | Fixed stranded PENDING reviews. A controller can reassign an unfinished, unexpired human review to a verified independent worker or escalate it. The deterministic bounded deadline sweep escalates at most one time and does not cancel or recreate the NF task or its hold. |
| Four presets and ceilings | tests/test_preset_fences.py | PASS. Preview + CAS preserves independently modified staff, verifies supervisor availability, fences old tenures and leaves already-held budget untouched. Operator policy controls headcount, concurrent held delegations and permitted role/team pairs. |
| Native manager and synthetic scene | integration/test_manager_resume.py; scene-minimal.json, scene-standard.json, cross-service-junit.xml | The scene fixture opens employee stdio clients, conveys a goal and scoped messages, creates an actual NF prerequisite through W3, exercises NF's existing three-role simulation and submits a frozen artifact for independent-role review. Host orchestration, deterministic prose fixtures and synthetic model adapters participate. This is an engineering closure rather than verification of a production author's literary workflow. |

## Clean W6 SHA capacity verification

A separate clean checkout at `d5bcb7cb0a1e9030b122561e32c90c0b182088ec` was migrated to its own head `w006`. Three fresh databases repeated the exact frozen contract. Raw records are in `clean-w006-rerun/messages-{10,50,200}.json`. Every scheduled message was durably acknowledged and reconstituted in each tier.

| Employees | Completed | Send P99 | ACK P99 | Stream recovery |
|---:|---:|---:|---:|---:|
| 10 | 30 / 30 | 67.35 ms | 68.14 ms | 0.25 s |
| 50 | 150 / 150 | 87.45 ms | 72.66 ms | 1.27 s |
| 200 | 600 / 600 | 106.58 ms | 55.70 ms | 5.23 s |

## Recovery review requiring a separate correction

Four defects were reported to the dedicated recovery implementer during this pass. Review the subsequent recovery patch and its JUnit before signing these paths off.

1. After committing a SENDING fence, a worker can die before NF receives the request. An authenticated original-key lookup returning explicit absence, with no local remote receipt or attempt observation, was left in perpetual lookup until escalation. Frozen lookup also advanced pristine REQUESTED rows to SENDING. Safe recovery requires preserving the original key, respecting the freeze, checking both sides' restore evidence and fencing the caller again before sending.
2. An author binding can become stale while the manager performs several original-key reconciliations. Revalidate the original authority and recovery fence before requesting NF resume. A stale/unknown outcome cannot release an already-sent hold.
3. A malformed partner aggregate could claim SETTLED while its attempt still says UNKNOWN or has missing actual cost. Refuse contradictory accounting and keep the hold. Restoration or receipt disagreement requires reconciliation before outbound dispatch.

4. Restoring NF to a point after creation of the same receipt but before model dispatch can retain the complete operation-to-receipt map while losing an already settled attempt. A comparison limited to receipt IDs is insufficient to permit a restart. Terminal operations need epoch-bound reads and agreement on generation, task, attempt identities, states, amounts and tokens. A mismatched or missing observation must keep dispatch fenced, including when a restored older NF task could otherwise be resumed. NF restoration separately fences previously pending tasks into PAUSED/BLOCKED and increments their generations; operators must keep provider/manager dispatch disabled until reconciled.

## Reproduction and limits

```sh
cd workshop
aw-migrate upgrade
python -m pytest tests -q
# In separate disposable aw_benchmark_* databases, one per employee tier:
python benchmarks/message_capacity.py --employees 200 --output result-200.json
```

Use a dedicated PostgreSQL 17 database and private Redis 8 stream. The recorded independent regression passed 40 tests with a live database, Redis and stdio. The identifiers/bytes in the per-file hashes are the execution evidence for this integration worktree; they do not establish that any later remote SHA passed. The historical failed delegation harness records remain alongside the subsequent tests. The local tests provide no external provider spending authorization, reviewer identities for a real novel, native author desktop sign-off, original NF capacity waiver or merge authority.
