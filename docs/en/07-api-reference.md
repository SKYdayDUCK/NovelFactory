# API reference

[中文](../zh-CN/07-接口参考.md) · [Runtime](05-agent-runtime.md) · [Configuration](08-configuration-and-deployment.md)

The checkout registers 34 NovelFactory tools, 34 Workshop tools, two installation delegation tools and 28 HTTP routes. Use the tools advertised to the identity bound to your host session. Each entry links to the request schema, a test-backed request and result, and the implementation.

## Reading the entries

Input fields with a trailing `?` are optional. Named types such as `WorldRef` refer to a schema definition. The linked definition supplies nested fields, limits, enumerations, defaults and conditional branches. A missing optional field follows the handler's behavior. Result fields show the JSON types observed in the linked example. A sample `null` describes that execution. Methods with multiple result forms list the conditions that select them. The example records identify their originating test. Fixture identifiers refer to the objects created by that test.

The examples cover 20 direct HTTP routes and eight HTTP input mappings paired with results from the corresponding application service. The MCP examples include 62 service fixtures, four direct Workshop handlers and four NovelFactory application results. To construct a JSON-RPC call, put the example request in `params.arguments`. For HTTP, use it as the JSON body of a POST and replace each UUID in the path on both GET and POST. The six examples with path parameters include the matching `pathBindings`.

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"nf_resume_project","arguments":{}}}
```

Call after the host has initialized a bound MCP session. On success, inspect `structuredContent`. An HTTP error object supplies `code`, `safe_message`, `retryable`, `request_id` and `recovery`. On a tool error, inspect `isError` and `structuredContent.error.code`. Common codes include `VIEW_DENIED`, `INVALID_ARGUMENT` and `SERVICE_UNAVAILABLE`. Workshop checks current employment and limits argument JSON to 32,768 UTF-8 bytes. A stale employee binding can yield `STALE_EMPLOYMENT`. The installation port checks enabled status, expiry, policy revision and live authority on each call.

Use a bearer credential for authenticated HTTP endpoints. The default request limit is 262,144 bytes with a five-second body deadline. Error responses include a request ID. Authentication and scope failures map to 401 and 403, missing resources to 404, body size and media type to 413 and 415, schema errors to 422, and most state conflicts to 409. A request deadline can return 408. Admission and availability errors at 429 or 503 may supply `Retry-After`. Check the call's replay rules before trying again. [HTTP adapter](../../src/novel_factory/interfaces/http.py)

## Operator and development entry points

| Entry | Use and authority |
| --- | --- |
| `aw-migrate`, `aw-provision`, `aw-admin` | Database owner or installation operator. Migrate Workshop, provision its installation, or administer credentials and grants. |
| `aw-mcp`, `aw-manager-run`, `aw-reconcile` | Serve the bound tools, dispatch approved work, or reconcile persisted assignments with their receipts. |
| `scripts/p58_bootstrap_book.py`, `scripts/provision_resume.py` | Operator-side project bootstrap and binding for author resume. Keep private paths separate for each book. |
| `scripts/run_supervisor.py`, `scripts/runtime_policy.py`, `scripts/runtime_limits.py` | Run a bound supervisor or administer operator policy and limits. See the runtime and deployment guides for prerequisites. |
| `scripts/p55_statistics.py`, `scripts/p55_chapter_costs.py`, `scripts/p6_reconcile_invoice.py` | Read operational statistics, trace chapter costs or reconcile separately supplied billing evidence. |
| `scripts/backup.py`, `scripts/pitr_drill.py` | Take a backup or rehearse recovery in isolation. See the recovery guide before selecting a target. |

The Python application classes back these transports. The benchmark modules create isolated measurements. An operator's configuration files supply provider registrations and routing rules. The model's tools cannot enroll themselves, allocate a budget or select new privileges. [Workshop entry points](../../workshop/pyproject.toml) · [Runtime guide](05-agent-runtime.md) · [Recovery](09-operations-and-recovery.md)

From a checkout with both services' development dependencies, inspect a registration or verify the catalog and its examples.

```bash
.venv/bin/python scripts/export_public_api.py --name nf_resume_project
.venv/bin/python scripts/export_public_api.py --check docs/reference/catalog.json --examples docs/reference/examples.json
```

## NovelFactory MCP

### `nf_artifact_put`

Store a task candidate with a stable idempotency key.

- **Access** Current task lease holder with ARTIFACT_WRITE and the frozen artifact_put tool.
- **Input** `content: string`, `idempotency_key: string`
- **Result** Returns CANDIDATE_STORED and artifact_ref for the candidate. `artifact_ref: object`, `status: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L1901) · [Request and result](../reference/examples.json#L2404) · [Handler](../../src/novel_factory/application/mcp_runtime.py#L367)

### `nf_change_point_request`

Record evidence for a bounded revision to the volume plan.

- **Access** A volume_director on a volume_review lease with volume-director-review, frozen plan_propose, PLAN_READ and ARTIFACT_WRITE.
- **Input** `volume: string`, `prior_plan_ref: object`, `event_refs: array`, `reason: string`, `idempotency_key: string`, `assessment?: object`
- **Result** Returns the change-point ArtifactRef for review. `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `BRANCH_CHANGED`, `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`, `NO_REPLAN_IMPACT`, `PLAN_SELECTION_CONFLICT`

[Registration](../reference/catalog.json#L1926) · [Request and result](../reference/examples.json#L2420) · [Handler](../../src/novel_factory/application/rolling_plans.py#L148)

### `nf_change_point_review`

Record an independent decision on the exact candidate and window.

- **Access** A chief_editor on an editorial_decision lease with review-change-point, frozen review_submit, PLAN_READ and ARTIFACT_WRITE. The editor must be independent of the change-point author.
- **Input** `change_point_ref: object`, `plan_ref: object`, `window_ref: object`, `decision: APPROVED|RETURNED`, `reason: string`, `idempotency_key: string`
- **Result** Returns a review ArtifactRef after checking the verdict against the current change point. `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `HEAD_CONFLICT`, `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L2138) · [Request and result](../reference/examples.json#L2460) · [Handler](../../src/novel_factory/application/rolling_plans.py#L407)

### `nf_context_get`

Read the bound task's authorized context.

- **Access** Current task lease holder with matching identity, generation, context and knowledge cut, the frozen tool and READ.
- **Input** `{}`
- **Result** Returns the frozen context bundle. Materials, limitations and actor_id depend on the role and knowledge cut. `facts: array`, `role_id: string`, `task_id: string`, `actor_id: string`, `world_ref: object`, `generation: integer`, `story_time: object`, `limitations: array`, `policy_version: string`, `schema_version: string`, `context_bundle_id: string`, `dependency_handle: string`, `dependency_fingerprint: string`, `permitted_evidence_refs: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L2242) · [Request and result](../reference/examples.json#L2489) · [Handler](../../src/novel_factory/application/mcp_runtime.py#L337)

### `nf_harness_agent_history`

Read the dynamic agent's authorized selection and lifecycle history.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and dynamic_agents must be enabled.
- **Input** `world_ref: object`, `instance_ref: object`
- **Result** Returns the current state and epoch together with the instance’s events and revisions. `instance_ref: object`, `epoch: integer`, `state: string`, `revision_ref: object|null`, `events: array`, `revisions: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `DYNAMIC_VERSION_CONFLICT`

[Registration](../reference/catalog.json#L2395) · [Request and result](../reference/examples.json#L2600) · [Handler](../../src/novel_factory/application/dynamic_agents.py#L628)

### `nf_harness_agent_register`

Register scoped profile and Skill text from a delegated source assignment.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and dynamic_agents must be enabled.
- **Input** `task_id: string`, `generation: integer`, `workshop_binding: object`, `profile_version: string`, `role_instructions: string`, `skill_instructions: string`, `requested_tools: array`, `trigger: SKILL_GAP|REVIEW_FINDING|TASK_DEMAND`, `evidence_refs: array`, `quota: object`, `idempotency_key: string`
- **Result** New and identically registered versions return instance_ref, revision_ref and proposal_ref. `instance_ref: object`, `revision_ref: object`, `proposal_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `DYNAMIC_INSTANCE_INACTIVE`, `DYNAMIC_VERSION_CONFLICT`, `FROZEN_SOURCE_MISMATCH`, `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L2457) · [Request and result](../reference/examples.json#L2652) · [Handler](../../src/novel_factory/application/dynamic_agents.py#L196)

### `nf_harness_agent_retire`

Exit or archive the scoped dynamic agent at its expected version.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and dynamic_agents must be enabled.
- **Input** `world_ref: object`, `instance_ref: object`, `action: EXIT|ARCHIVE`, `expected_epoch: integer`, `idempotency_key: string`
- **Result** EXIT or ARCHIVE returns the resulting state and event after checking expected_epoch. `instance_ref: object`, `revision_ref: null (both retirement actions)`, `epoch: integer`, `state: string`, `event_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `DYNAMIC_INSTANCE_INACTIVE`, `DYNAMIC_SELECTION_CONFLICT`, `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L2672) · [Request and result](../reference/examples.json#L2709) · [Handler](../../src/novel_factory/application/dynamic_agents.py#L583)

### `nf_harness_agent_select`

Select a registered revision for new assignments, including a historical revision.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and dynamic_agents must be enabled.
- **Input** `world_ref: object`, `instance_ref: object`, `revision_ref: object`, `expected_epoch: integer`, `idempotency_key: string`
- **Result** Returns the selected revision, state, event and advanced epoch; checks expected_epoch. `instance_ref: object`, `revision_ref: object`, `epoch: integer`, `state: string`, `event_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `DYNAMIC_INSTANCE_INACTIVE`, `DYNAMIC_SELECTION_CONFLICT`, `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L2752) · [Request and result](../reference/examples.json#L2743) · [Handler](../../src/novel_factory/application/dynamic_agents.py#L478)

### `nf_harness_instruction_case`

Describe the exact participants and artifacts for an independent registrar.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled.
- **Input** `world_ref: object`, `candidate_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `reviewer_task_id: string`, `reviewer_generation: integer`, `publisher_grant_id: string`
- **Result** Returns the bound candidate, evaluation, reviewer and publisher for independence verification. `kind: string`, `authority: string`, `world_ref: object`, `candidate_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `freeze_ref: object`, `producers: array`, `reviewer: object`, `publisher: object`, `reviewer_task_id: string`, `reviewer_generation: integer`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `INDEPENDENT_REVIEW_REQUIRED`

[Registration](../reference/catalog.json#L2849) · [Request and result](../reference/examples.json#L2785) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L220)

### `nf_harness_instruction_decide`

Decide whether to release independently reviewed instructions.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled. The publisher must match the evaluation and current independent opinion. Automatic release also requires a live policy and auto_promote.
- **Input** `candidate_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `world_ref: object`, `review_ref: object`, `action: APPROVE|REJECT`, `expected_epoch: integer`, `reason: string`, `valid_until: string`, `idempotency_key: string`, `route?: MANUAL|POLICY_AUTOMATIC`, `policy_ref?: object`
- **Result** APPROVE can advance the selection. REJECT records the decision and returns the current selection, which can be null. `epoch: integer`, `action: string`, `decision_ref: object`, `selection_ref: object|null`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `CAPACITY_LIMIT`, `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`, `PROMOTION_NOT_AUTHORIZED`, `PROMOTION_REVOKED`, `SELECTION_CONFLICT`

[Registration](../reference/catalog.json#L2974) · [Request and result](../reference/examples.json#L2872) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L707)

### `nf_harness_instruction_evaluate`

Run every preregistered arm and retain outcomes for independent review.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled.
- **Input** `world_ref: object`, `candidate_ref: object`, `idempotency_key: string`
- **Result** Returns an engineering report and run manifest. On replay, eligible can be revalidated against current evidence. `candidate_ref: object`, `freeze_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `status: string`, `eligible: boolean`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `ADAPTIVE_INSTRUCTIONS_DISABLED`, `EVALUATION_FREEZE_MISMATCH`, `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`

[Registration](../reference/catalog.json#L3166) · [Request and result](../reference/examples.json#L2922) · [Handler](../../src/novel_factory/application/adaptive_evaluation.py#L645)

### `nf_harness_instruction_evidence`

Read the assigned evaluation outcomes and scope.

- **Access** Assigned reviewer, literary_editor or chief_editor independent of authors and evaluator; the lease requires ARTIFACT_WRITE, frozen review_submit and both Creative Harness and adaptive_instructions enabled.
- **Input** `candidate_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`
- **Result** Returns arm totals and the available measured fields of each case. `freeze_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `eligible: boolean`, `status: string`, `quality: object`, `arm_totals: object`, `candidate_ref: object`, `change_kind: string`, `target: object`, `cases: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `INDEPENDENT_REVIEW_REQUIRED`

[Registration](../reference/catalog.json#L3234) · [Request and result](../reference/examples.json#L2963) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L297)

### `nf_harness_instruction_monitor`

Read NF attempts and settlement for a task that froze a specific release.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled.
- **Input** `world_ref: object`, `decision_ref: object`, `task_id: string`, `generation: integer`, `idempotency_key: string`
- **Result** Persists one observation and returns attempts, known cost, unsettled count, observed time and monitor_ref. `monitor_ref: object`, `decision_ref: object`, `selection_ref: object`, `task_id: string`, `generation: integer`, `task_state: string`, `result_id: string|null`, `observed_at: string`, `attempts: array`, `known_cost: string`, `unsettled_attempts: integer`, `literary_status: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`, `STALE_GENERATION`

[Registration](../reference/catalog.json#L3319) · [Request and result](../reference/examples.json#L3125) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L1064)

### `nf_harness_instruction_policy`

Authorize a scoped publisher under an expiring automatic promotion policy.

- **Access** Branch-bound control_operator with ADMIN and the Creative Harness, adaptive_instructions and auto_promote switches enabled.
- **Input** `world_ref: object`, `role_id: string`, `task_kind: string`, `skill_name: string`, `dynamic_instance_ref?: object`, `publisher_grant_id: string`, `valid_until: string`, `idempotency_key: string`
- **Result** Returns policy_ref for a target-scoped, time-bounded policy. `policy_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L3397) · [Request and result](../reference/examples.json#L3177) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L496)

### `nf_harness_instruction_propose`

Freeze a scoped instruction and comparison using signed manuscript reviews.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled.
- **Input** `world_ref: object`, `task_id: string`, `generation: integer`, `failure_report_refs: array`, `success_report_refs: array`, `evaluation_report_refs?: array`, `change_kind: ROLE_INSTRUCTIONS|SKILL_INSTRUCTIONS|CONTEXT_RECIPE|REVIEW_RUBRIC`, `quota: object`, `order_seed: integer`, `repetitions: integer`, `instructions?: string`, `material_order?: array`, `idempotency_key: string`
- **Result** Returns references to the candidate instruction, baseline and frozen experiment design for separate evaluation. `candidate_ref: object`, `experiment_ref: object`, `design_ref: object`, `baseline_ref: object`, `instruction_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `CAPACITY_LIMIT`, `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`

[Registration](../reference/catalog.json#L3492) · [Request and result](../reference/examples.json#L3201) · [Handler](../../src/novel_factory/application/adaptive_instructions.py#L870)

### `nf_harness_instruction_review`

Record an independent opinion on the assigned experiment.

- **Access** Assigned reviewer, literary_editor or chief_editor independent of authors and evaluator; the lease requires ARTIFACT_WRITE, frozen review_submit and both Creative Harness and adaptive_instructions enabled.
- **Input** `candidate_ref: object`, `engineering_report_ref: object`, `run_manifest_ref: object`, `publisher_grant_id: string`, `verdict: APPROVE|REJECT`, `reason: string`, `valid_until: string`, `idempotency_key: string`
- **Result** Returns the independent opinion’s review_ref and verdict. `review_ref: object`, `verdict: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `PROMOTION_REVOKED`

[Registration](../reference/catalog.json#L3724) · [Request and result](../reference/examples.json#L3279) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L359)

### `nf_harness_instruction_revoke_policy`

Withdraw the scoped authority for subsequent automatic releases.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled.
- **Input** `world_ref: object`, `policy_ref: object`, `reason: string`, `idempotency_key: string`
- **Result** Returns the revocation and original policy references. Revocation remains available when auto_promote is off. `revocation_ref: object`, `policy_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L3838) · [Request and result](../reference/examples.json#L3313) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L608)

### `nf_harness_instruction_rollback`

Restore the previous scoped selection at the expected release epoch.

- **Access** Branch-bound control_operator with ADMIN; Creative Harness and adaptive_instructions must be enabled. The publisher must satisfy the original decision’s rollback authority.
- **Input** `world_ref: object`, `decision_ref: object`, `expected_epoch: integer`, `reason: string`, `review_ref?: object`, `idempotency_key: string`
- **Result** Restores the preceding selection and advances its epoch. restored_selection_ref can be null if there was no previous selection. `decision_ref: object`, `restored_selection_ref: object|null`, `selection_ref: object|null`, `epoch: integer`, `action: string`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `PROMOTION_NOT_AUTHORIZED`, `SELECTION_CONFLICT`

[Registration](../reference/catalog.json#L3912) · [Request and result](../reference/examples.json#L3343) · [Handler](../../src/novel_factory/application/adaptive_promotion.py#L932)

### `nf_harness_metrics`

Read measured creative workflow activity in the bound work, branch and time window.

- **Access** A control_operator with ADMIN on the bound work and branch; Creative Harness must be enabled.
- **Input** `world_ref: object`, `since?: string`, `until?: string`, `limit?: integer`
- **Result** Aggregates recorded metrics for a WorldRef and filters; coverage and unknown values depend on available records. `schema_version: string`, `world_ref: object`, `coverage: object`, `context: object`, `planning: object`, `review: object`, `dynamic: object`, `instructions: object`, `scenes: array`, `latency: object`, `recovery: object`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L4014) · [Request and result](../reference/examples.json#L3373) · [Handler](../../src/novel_factory/application/creative_metrics.py#L317)

### `nf_harness_plan_decide`

Submit a plan decision to the existing adoption service, or decline every alternative.

- **Access** A control_operator with ADMIN on the bound work and branch; Creative Harness must be enabled. Selecting an option also requires APPROVE and a principal independent of the planner.
- **Input** `task_id: string`, `generation: integer`, `candidate_ref: object`, `option_id: string|null`, `rolling_review_ref?: object|null`, `idempotency_key: string`
- **Result** A selected option requires independent approval authority. Adoption returns ADOPTED. A decline returns DECLINED with null plan_ref and adoption. `status: string`, `plan_ref: object|null`, `candidate_ref: object`, `option_id: string|null`, `adoption: object|null`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`, `PLAN_SELECTION_CONFLICT`

[Registration](../reference/catalog.json#L4065) · [Request and result](../reference/examples.json#L3602) · [Handler](../../src/novel_factory/application/creative_planning.py#L265)

### `nf_harness_plan_propose`

Derive bounded conditional alternatives from the assigned plan.

- **Access** A volume_director with a PLAN-plane Creative Harness lease, frozen plan_propose, PLAN_READ and ARTIFACT_WRITE.
- **Input** `plan_ref: object`, `node_ids: array`, `trigger: ORDINARY|MOTIVE_CONFLICT|IRREVERSIBLE`, `quota: object`, `horizon_scenes?: integer`, `idempotency_key: string`
- **Result** Returns the candidate and per-option plan references for a later adoption decision. `candidate_ref: object`, `candidate: object`, `option_plan_refs: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `CAPACITY_LIMIT`, `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L4160) · [Request and result](../reference/examples.json#L3629) · [Handler](../../src/novel_factory/application/creative_planning.py#L208)

### `nf_harness_review_decide`

Record an authorized editorial response to the cited draft and review.

- **Access** A control_operator with ADMIN on the bound work and branch; Creative Harness must be enabled. The editor also needs APPROVE and must be independent of the reviewer and producers.
- **Input** `task_id: string`, `generation: integer`, `report_ref: object`, `action: RETURN|RETAIN|ACCEPT_REVISION|DEFER|WITHDRAW`, `reason: string`, `native_decision?: object`, `idempotency_key: string`
- **Result** Final editorial actions require independence from authors and reviewers, a native decision and current proof. Non-final actions can have null native_decision_ref. `action: string`, `decision_ref: object`, `native_decision_ref: object|null`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `EDITORIAL_BINDING_MISMATCH`, `EDITORIAL_DECISION_REQUIRED`, `EDIT_CONFLICT`, `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `REVIEW_REQUIRED`, `REWORK_LIMIT`

[Registration](../reference/catalog.json#L4280) · [Request and result](../reference/examples.json#L3778) · [Handler](../../src/novel_factory/application/creative_review.py#L429)

### `nf_harness_review_independence`

Verify the named review assignment and manuscript against trusted subject records.

- **Access** A control_operator with ADMIN on the bound work and branch; Creative Harness must be enabled.
- **Input** `task_id: string`, `generation: integer`, `manuscript_ref: object`
- **Result** Validates manuscript producers and the review assignment; returns a persisted independence_ref. `independence_ref: object`
- **Retry** Rechecks live independence. The same binding, evidence digest and issuer epoch reuse the existing NF proof artifact.
- **Errors** `INDEPENDENT_REVIEW_REQUIRED`

[Registration](../reference/catalog.json#L5212) · [Request and result](../reference/examples.json#L3803) · [Handler](../../src/novel_factory/application/review_independence.py#L249)

### `nf_harness_review_read`

Read the exact assigned manuscript and obtain a coverage receipt.

- **Access** Creative Harness task lease for a reviewer, literary_editor, chief_editor or writer with ARTIFACT_WRITE and frozen evidence_read.
- **Input** `draft_ref: object`, `idempotency_key: string`
- **Result** Returns the authorized text, span, delivery_ref and coverage_ref; persists text delivery and coverage evidence. `text: string`, `text_ref: object`, `draft_ref: object`, `scene_ref: object`, `world_ref: object`, `span: object`, `delivery_ref: object`, `coverage_ref: object`
- **Retry** The same key and request redeliver the authorized text and reuse the original coverage_ref. Each text delivery has its own delivery record.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_READING_COVERAGE`

[Registration](../reference/catalog.json#L5259) · [Request and result](../reference/examples.json#L3823) · [Handler](../../src/novel_factory/application/creative_review.py#L176)

### `nf_harness_review_submit`

Report precisely located literary findings for the assigned manuscript.

- **Access** Creative Harness task lease for a reviewer, literary_editor or chief_editor with ARTIFACT_WRITE, frozen review_submit and the assignment’s independence checks.
- **Input** `binding: object`, `target_ref: object`, `independence_ref: object`, `related_independence_refs?: array`, `scope_draft_refs: array`, `coverage_refs: array`, `layer: WORLD_SETTING|NAMING|PLOT|STAGING|CHARACTER|PROSE|CHAPTER_SEAM|VOLUME|FACT_KNOWLEDGE`, `reader_position?: NO_OPINION|NO_ISSUES`, `observations: array`, `quota: object`, `rework_limit: integer`, `prior_report_ref?: object|null`, `idempotency_key: string`
- **Result** Returns the native review, detailed report and findings. The native outcome can be CHANGES_REQUESTED, UNDETERMINED or PASS. `report_ref: object`, `native_review_ref: object`, `details_ref: object`, `report: object`, `findings: array`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `BUDGET_EXCEEDED`, `CAPACITY_LIMIT`, `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `INVALID_EVIDENCE`, `READING_COVERAGE_INCOMPLETE`, `REVIEW_REVOKED`

[Registration](../reference/catalog.json#L5302) · [Request and result](../reference/examples.json#L3874) · [Handler](../../src/novel_factory/application/creative_review.py#L187)

### `nf_harness_review_withdraw`

Withdraw a review written by this assigned reviewer.

- **Access** Creative Harness task lease for a reviewer, literary_editor or chief_editor with ARTIFACT_WRITE, frozen review_submit and the assignment’s independence checks.
- **Input** `report_ref: object`, `reason: string`, `idempotency_key: string`
- **Result** The original reviewer can withdraw their report. Returns WITHDRAW; native_decision_ref can be null. `action: string`, `decision_ref: object`, `native_decision_ref: null`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `REVIEW_REVOKED`

[Registration](../reference/catalog.json#L5936) · [Request and result](../reference/examples.json#L4029) · [Handler](../../src/novel_factory/application/creative_review.py#L429)

### `nf_harness_revise`

Revise only the assigned finding spans and retain the source draft.

- **Access** Writer holding a targeted_revision lease, with Creative Harness, ARTIFACT_WRITE and frozen draft_submit and artifact_put.
- **Input** `report_ref: object`, `scene_ref: object`, `replacements: array`, `idempotency_key: string`
- **Result** Returns the revised draft_ref, revision evidence and new round. `draft_ref: object`, `revision_ref: object`, `round: integer`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `EDITORIAL_NO_PROGRESS`, `IDEMPOTENCY_MISMATCH`, `INVALID_EVIDENCE`

[Registration](../reference/catalog.json#L5985) · [Request and result](../reference/examples.json#L4051) · [Handler](../../src/novel_factory/application/creative_review.py#L518)

### `nf_harness_revision_brief`

Read the returned author-facing findings for the assigned manuscript.

- **Access** Writer holding a targeted_revision lease, with Creative Harness, ARTIFACT_WRITE and frozen draft_submit and artifact_put.
- **Input** `report_ref: object`
- **Result** Returns the authorized passages, excerpts and next revision round for a returned manuscript. `report_ref: object`, `draft_ref: object`, `text_ref: object`, `round: integer`, `findings: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L6076) · [Request and result](../reference/examples.json#L4087) · [Handler](../../src/novel_factory/application/creative_revision_brief.py#L88)

### `nf_harness_simulate`

Evaluate a frozen candidate using enrolled actors' fixed knowledge cuts.

- **Access** A control_operator with ADMIN on the bound work and branch; Creative Harness must be enabled.
- **Input** `task_id: string`, `generation: integer`, `candidate_ref: object`, `actor_tasks: array`, `max_branches: integer`, `max_steps_per_branch: integer`, `seed: integer`, `idempotency_key: string`
- **Result** The report, usage and stop reasons describe candidate branches. Simulation outcomes remain hypotheses. `report_ref: object`, `report: object`, `usage: object`, `stops: array`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `TASK_REBIND_REQUIRED`

[Registration](../reference/catalog.json#L6113) · [Request and result](../reference/examples.json#L4135) · [Handler](../../src/novel_factory/application/creative_simulation.py#L314)

### `nf_project_status`

Read task counts for the configured project branch.

- **Access** A resume_operator bound exclusively to this work and branch, with PROJECT_RESUME as its sole capability.
- **Input** `{}`
- **Result** Returns branch counts, workflow state, blockers, pending reviews and the resume grant expiry. `counts: object`, `ready_count: integer`, `workflow_state: string`, `blockers: object`, `pending_review_count: integer`, `pending_author_approval_count: integer`, `resume_grant_expires_at: string`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L6209) · [Request and result](../reference/examples.json#L4294) · [Handler](../../src/novel_factory/orchestration/workflow.py#L147)

### `nf_query_batch`

Read selected state values at this task's bound time and knowledge cut.

- **Access** Current task lease holder with matching identity, generation, context and knowledge cut, the frozen tool and READ. STATE lookups are limited to subject_id/predicate pairs permitted by the frozen input manifest.
- **Input** `lookups: array`
- **Result** Lookups must be in the task manifest. SUCCESS items include payload, freshness and dependency_handle; ERROR items contain an error. `items: array`, `all_items_succeeded: boolean`
- **Retry** Repeatable. Each delivered successful lookup records a semantic dependency for the task generation.
- **Errors** `CAPACITY_LIMIT`

[Registration](../reference/catalog.json#L6220) · [Request and result](../reference/examples.json#L4312) · [Handler](../../src/novel_factory/application/mcp_runtime.py#L343)

### `nf_resume_project`

Recover eligible work and wake satisfied waits in the configured branch.

- **Access** A resume_operator bound exclusively to this work and branch, with PROJECT_RESUME as its sole capability.
- **Input** `{}`
- **Result** Also returns awakened, recovered, closed_waits and expired_ready counts from this pass. `counts: object`, `ready_count: integer`, `workflow_state: string`, `blockers: object`, `pending_review_count: integer`, `pending_author_approval_count: integer`, `awakened: integer`, `recovered: integer`, `closed_waits: integer`, `expired_ready: integer`, `resume_grant_expires_at: string`
- **Retry** Repeat to process currently eligible or newly expired work. Counts describe that pass; there is no caller-supplied idempotency key.
- **Errors** common errors

[Registration](../reference/catalog.json#L6258) · [Request and result](../reference/examples.json#L4365) · [Handler](../../src/novel_factory/orchestration/workflow.py#L223)

### `nf_rolling_window_submit`

Submit a bounded future window for independent review.

- **Access** A volume_director on a replan lease with roll-window, frozen plan_propose, PLAN_READ and ARTIFACT_WRITE.
- **Input** `change_point_ref: object`, `window: object`, `idempotency_key: string`
- **Result** Returns plan_ref, window_ref and the associated change_point_ref. `plan_ref: object`, `window_ref: object`, `change_point_ref: object`
- **Retry** Retry with the original idempotency_key and payload. A matching receipt can be replayed under current authority; changing the payload conflicts.
- **Errors** `HEAD_CONFLICT`, `IDEMPOTENCY_MISMATCH`, `WINDOW_CONFLICT`

[Registration](../reference/catalog.json#L6269) · [Request and result](../reference/examples.json#L4387) · [Handler](../../src/novel_factory/application/rolling_plans.py#L280)

### `nf_task_get`

Read the bound task's current execution identity.

- **Access** Current task lease holder with matching identity, generation, context and knowledge cut, the frozen tool and READ.
- **Input** `{}`
- **Result** Returns the bound task’s ID, generation, state, context ID and WorldRef. `task_id: string`, `generation: integer`, `state: string`, `context_bundle_id: string`, `world_ref: object`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L6316) · [Request and result](../reference/examples.json#L4464) · [Handler](../../src/novel_factory/application/mcp_runtime.py#L330)

## Workshop MCP

### `aw_capabilities`

Negotiate this connection's organization protocol.

- **Access** Authenticated Workshop installation or employee session.
- **Input** `protocol_version: string`
- **Result** Returns the installation, instance, grant epoch, catalog binding and negotiated limits. `protocol_version: string`, `installation_id: string`, `nf_instance_id: string`, `grant_epoch: integer`, `max_argument_bytes: integer`, `max_ttl_seconds: integer`, `nf_commit: string`, `role_catalog_hash: string`, `skill_catalog_hash: string`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `PROTOCOL_UNSUPPORTED`

[Registration](../reference/catalog.json#L312) · [Request and result](../reference/examples.json#L1574) · [Handler](../../workshop/src/agent_workshop/server.py#L21)

### `aw_delegate`

Reserve organization budget and request a provisioned NF task slot.

- **Access** Current employee with a delegation route matching slot, recipient, installation and scope.
- **Input** `operation_id: string`, `slot_id: string`, `max_amount: string`, `token_limit: integer`
- **Result** A new delegation returns REQUESTED. A replay can report a later state. `operation_id: string`, `state: string`
- **Retry** Reuse the caller’s operation_id and arguments under the same installation, organization, sender and employment revision. Use the returned server-generated operation_id to query progress.
- **Errors** `ACTIVE_DELEGATION_LIMIT`, `BUDGET_EXCEEDED`, `BUDGET_UNCONFIGURED`, `IDEMPOTENCY_MISMATCH`, `REVIEW_REQUEST_REQUIRED`, `SLOT_ALREADY_ASSIGNED`

[Registration](../reference/catalog.json#L331) · [Request and result](../reference/examples.json#L1592) · [Handler](../../workshop/src/agent_workshop/delegation.py#L43)

### `aw_delivery_ack`

Acknowledge an addressed message.

- **Access** The current recipient and employment revision must match; delivery, sender binding and route must still be live.
- **Input** `message_id: string`
- **Result** Returns ACKNOWLEDGED, the sequence number and first acknowledgement time. `status: string`, `message_id: string`, `sequence: integer`, `acknowledged_at: string`
- **Retry** Acknowledge by message_id. An authorized replay preserves the first acknowledgement timestamp.
- **Errors** `DELIVERY_EXPIRED`, `DELIVERY_UNAVAILABLE`, `ROUTE_REVOKED`

[Registration](../reference/catalog.json#L366) · [Request and result](../reference/examples.json#L1606) · [Handler](../../workshop/src/agent_workshop/messaging.py#L456)

### `aw_dynamic_skill_attest`

Verify the current employee and creative Skill and optionally freeze it to the exact delegated NF task.

- **Access** Current employee meeting the skill binding’s live owner, mode, scope and sharing policy checks. The NF role must match the Workshop employee. Task attestation additionally verifies the installation, delegation, slot and receipt.
- **Input** `binding_id: string`, `nf: object`, `task?: object`, `freeze_task: boolean`
- **Result** Without a task the result has task=null. With a task it verifies the delegation receipt; successful optional freezing sets frozen=true. `binding: object`, `nf: object`, `task: object|null`, `frozen: boolean`, `skill_epoch: integer`, `expires_at: string`, `skill_mode: string`
- **Retry** Re-attests on every call. With freeze_task=true a task is required and its skill binding is frozen.
- **Errors** `DYNAMIC_SKILL_DENIED`, `SKILL_DENIED`, `TASK_SKILL_ALREADY_FROZEN`

[Registration](../reference/catalog.json#L385) · [Request and result](../reference/examples.json#L1619) · [Handler](../../workshop/src/agent_workshop/dynamic_skills.py#L40)

### `aw_end`

End an employee using organization revision CAS.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `expected_revision: integer`, `employee_id: string`
- **Result** Returns organization and employment revisions after ending a tenure. `org_id: string`, `revision: integer`, `employee_id: string`, `employment_revision: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_EMPLOYMENT_TRANSITION`, `STALE_ORG_REVISION`

[Registration](../reference/catalog.json#L497) · [Request and result](../reference/examples.json#L1665) · [Handler](../../workshop/src/agent_workshop/organization.py#L280)

### `aw_handoff`

Transfer organization follow-up responsibility. The NF attempt keeps its identity.

- **Access** Current employee with a delegation route matching slot, recipient, installation and scope. The delegation must belong to the installation; the handler checks the current owner or an authorized replay by the sender.
- **Input** `operation_id: string`, `handoff_id: string`, `recipient: string`
- **Result** Returns the delegation ID, handoff ID and recipient after checking ownership and the receiving employee. `operation_id: string`, `handoff_id: string`, `recipient: string`
- **Retry** Use the same operation_id, handoff_id and recipient. A different recipient conflicts; retries recheck the route.
- **Errors** `IDEMPOTENCY_MISMATCH`

[Registration](../reference/catalog.json#L526) · [Request and result](../reference/examples.json#L1680) · [Handler](../../workshop/src/agent_workshop/delegation.py#L141)

### `aw_hire`

Hire an employee using organization revision CAS.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `expected_revision: integer`, `employee_id: string`, `role_id: string`, `supervisor_id: string`, `team_id: string`, `slot_id?: string`, `display_name?: string`
- **Result** Returns the new roster revision and the hired employee’s tenure revision. `org_id: string`, `revision: integer`, `employee_id: string`, `employment_revision: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `EMPLOYEE_ALREADY_REGISTERED`, `IDEMPOTENCY_MISMATCH`, `INCOMPATIBLE_ROLE`, `INVALID_DISPLAY_NAME`, `INVALID_SUPERVISOR`, `ROSTER_LIMIT_EXCEEDED`, `ROSTER_SCOPE_DENIED`, `SLOT_OCCUPIED`, `STALE_ORG_REVISION`

[Registration](../reference/catalog.json#L555) · [Request and result](../reference/examples.json#L1694) · [Handler](../../workshop/src/agent_workshop/organization.py#L280)

### `aw_history_get`

Read a fixed cut of organization history within the live audience grant.

- **Access** Current organization member and employment revision; reads are scoped to the caller’s audience.
- **Input** `cursor?: string`, `limit?: integer`
- **Result** The first page pins source_cut. Without a watermark, returns zero, an empty event list and null cursor. Subsequent pages use this audience’s cursor. `source_cut: integer`, `events: array`, `next_cursor: string|null`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `INVALID_CURSOR`

[Registration](../reference/catalog.json#L622) · [Request and result](../reference/examples.json#L1712) · [Handler](../../workshop/src/agent_workshop/manager.py#L23)

### `aw_mailbox_get`

Resume the authenticated employee's mailbox after a sequence.

- **Access** Current organization member and employment revision; reads are scoped to the caller’s audience.
- **Input** `after?: integer`, `limit?: integer`
- **Result** Items can be QUEUED, ACKNOWLEDGED or UNAVAILABLE. Unavailable items expose only sequence and status. Empty pages preserve after. `items: array`, `next_cursor: integer`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L644) · [Request and result](../reference/examples.json#L1772) · [Handler](../../workshop/src/agent_workshop/messaging.py#L471)

### `aw_message_body_get`

Read an addressed body under current route authorization.

- **Access** The current recipient and employment revision must match; delivery, sender binding and route must still be live.
- **Input** `message_id: string`
- **Result** Returns content after validating the live delivery. `content: string`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `BODY_DENIED`, `DELIVERY_EXPIRED`, `DELIVERY_UNAVAILABLE`, `ROUTE_REVOKED`

[Registration](../reference/catalog.json#L666) · [Request and result](../reference/examples.json#L1781) · [Handler](../../workshop/src/agent_workshop/messaging.py#L438)

### `aw_message_body_put`

Register a private body addressed to a recipient and current tenure.

- **Access** Current employee with a live route for the recipient, message kind and scope.
- **Input** `operation_id: string`, `recipient: string`, `scope_ref: string`, `content: string`
- **Result** Returns a body reference bound to recipient, employment revision and scope. `body_ref: string`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_BODY`, `ROUTE_DENIED`

[Registration](../reference/catalog.json#L685) · [Request and result](../reference/examples.json#L1791) · [Handler](../../workshop/src/agent_workshop/messaging.py#L148)

### `aw_message_send`

Send one addressed message over a permitted direction and message kind.

- **Access** Current employee with a live route for the recipient, message kind and scope.
- **Input** `operation_id: string`, `recipient: string`, `scope_ref: string`, `kind: string`, `graph_revision: integer`, `ttl_seconds: integer`, `body_ref?: string`, `route_grant_id?: string`, `causation_id?: string`, `incident_code?: SERVICE_OFFLINE|STALE_LEASE|RECEIPT_MISSING|QUEUE_PRESSURE`
- **Result** Returns a message ID, status and delivery sequence. Incident messages have separate kind and rate checks. `message_id: string`, `status: string`, `sequence: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `BODY_DENIED`, `CAUSATION_DENIED`, `IDEMPOTENCY_MISMATCH`, `INCIDENT_RATE_LIMIT`, `INVALID_INCIDENT`, `ROUTE_DENIED`, `ROUTE_HOP_LIMIT`, `SENDER_BINDING_UNVERIFIED`, `STALE_ORG_REVISION`

[Registration](../reference/catalog.json#L720) · [Request and result](../reference/examples.json#L1804) · [Handler](../../workshop/src/agent_workshop/messaging.py#L268)

### `aw_model_policy_get`

Export the current manager-approved model policy for this branch.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `work_id: string`, `branch_id: string`
- **Result** Returns the latest revision and rules for the organization, installation, work and branch. `org_id: string`, `installation_id: string`, `work_id: string`, `branch_id: string`, `revision: integer`, `rules: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `POLICY_UNAVAILABLE`

[Registration](../reference/catalog.json#L800) · [Request and result](../reference/examples.json#L1821) · [Handler](../../workshop/src/agent_workshop/model_policies.py#L48)

### `aw_model_policy_publish`

Publish a scoped policy revision for authorized NF adoption.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `work_id: string`, `branch_id: string`, `expected_revision: integer`, `rules: array`
- **Result** Returns the stored policy revision and rules, including on immediate replay. `org_id: string`, `installation_id: string`, `work_id: string`, `branch_id: string`, `revision: integer`, `rules: array`
- **Retry** Identical rules with the immediately preceding expected_revision replay. Read the current policy before publishing after intervening revisions.
- **Errors** `INVALID_ROUTING_RULES`, `STALE_REVISION`

[Registration](../reference/catalog.json#L824) · [Request and result](../reference/examples.json#L1843) · [Handler](../../workshop/src/agent_workshop/model_policies.py#L29)

### `aw_preset_apply`

Apply a matching preview at the current organization revision.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `preset: string`, `operation_id: string`, `expected_revision: integer`, `preview_sha: string`
- **Result** An already active preset returns only revision and preset. A switch also reports added, suspended and reactivated entries. `revision: integer`, `preset: string`, `added: array (switch)`, `suspended: array (switch)`, `reactivated: array (switch)`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `MANAGER_REQUIRED`, `PRESET_CONFLICT`, `STALE_PREVIEW`

[Registration](../reference/catalog.json#L1028) · [Request and result](../reference/examples.json#L1873) · [Handler](../../workshop/src/agent_workshop/presets.py#L115)

### `aw_preset_preview`

Preview roster, tenure and supervision changes.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `preset: string`
- **Result** Returns additions, suspensions, reactivations, blockers and the preview_sha. `org_id: string`, `revision: integer`, `from: string`, `to: string`, `add: array`, `suspend: array`, `reactivate: array`, `blocked: array`, `preview_sha: string`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L1067) · [Request and result](../reference/examples.json#L1894) · [Handler](../../workshop/src/agent_workshop/presets.py#L110)

### `aw_progress_get`

Read an authorized scene or role delegation and its latest NF receipt.

- **Access** A workshop_manager or author on this installation, or the delegation’s sender or owner at the matching employment revision.
- **Input** `operation_id: string`
- **Result** Management also sees task_id, generation, world_ref, accounting and hold state. Task fields can be null before the first remote receipt. `slot_id: string`, `owner_id: string`, `state: string`, `last_error: string|null`, `task_state: string|null`, `task_id: string|null (management)`, `generation: integer|null (management)`, `world_ref: object|null (management)`, `accounting_state: string|null (management)`, `hold: string (management)`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L1091) · [Request and result](../reference/examples.json#L1916) · [Handler](../../workshop/src/agent_workshop/manager.py#L132)

### `aw_resume_project`

Persist one authorized wake request for the bound NF project and end this turn.

- **Access** Current workshop_manager or author using this installation’s employee session.
- **Input** `operation_id: string`
- **Result** New requests start PENDING. Replays can return an already processed request and its result. `request_id: string`, `state: string`, `result: object|null`
- **Retry** Deduplicated by installation, employee tenure and operation_id. A retry returns the request’s current state and result.
- **Errors** common errors

[Registration](../reference/catalog.json#L1110) · [Request and result](../reference/examples.json#L1930) · [Handler](../../workshop/src/agent_workshop/manager.py#L167)

### `aw_review_decide`

Record a bounded decision on a frozen artifact and authorized evidence.

- **Access** The assigned reviewer at the bound employment revision, with substantive independence and access to the review evidence.
- **Input** `operation_id: string`, `review_id: string`, `outcome: APPROVED|CHANGES_REQUESTED`, `reason: CAUSALITY|VOICE|MOTIVATION|SCENE_ORDER|RESOURCE|RELIABILITY|ACCOUNTING|EVIDENCE`, `evidence_ref: string`
- **Result** Returns APPROVED, CHANGES_REQUESTED or ESCALATED. Expiry and a second round of requested changes trigger escalation. `review_id: string`, `state: string`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `EVIDENCE_SCOPE_DENIED`, `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `INVALID_REVIEW`, `REVIEW_DENIED`, `WAITING_NF_RECONCILIATION`

[Registration](../reference/catalog.json#L1129) · [Request and result](../reference/examples.json#L1942) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L650)

### `aw_review_get`

Read this assignment with evidence references filtered to its current reviewer.

- **Access** A current workshop_manager, review_lead or the assigned reviewer, within the applicable scope and team.
- **Input** `review_id: string`
- **Result** The assigned reviewer additionally receives artifact identity, revision, content digest, evidence and risk reason. `mode: string`, `state: string`, `round_no: integer`, `reviewer_id: string`, `execution: string`, `policy_revision: string`, `review_id: string`, `deadline: string`, `artifact_ref: string (assigned)`, `artifact_revision: string (assigned)`, `content_sha: string (assigned)`, `evidence_refs: array (assigned)`, `risk_reason: string (assigned)`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `REVIEW_DENIED`

[Registration](../reference/catalog.json#L1178) · [Request and result](../reference/examples.json#L1957) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L517)

### `aw_review_independence`

Verify a frozen NF draft and reviewer assignment against current Workshop authority.

- **Access** Current workshop_manager or review_lead in the review scope. A review_lead is restricted to the team they supervise.
- **Input** `binding: object`
- **Result** Returns the binding digest, review ID, policy revision, subject commitment and earliest expiry. `binding_sha256: string`, `review_id: string`, `policy_revision: string`, `subject_commitment: string`, `expires_at: string`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `INDEPENDENT_REVIEW_REQUIRED`, `REVIEW_DENIED`

[Registration](../reference/catalog.json#L1197) · [Request and result](../reference/examples.json#L1974) · [Handler](../../workshop/src/agent_workshop/review_independence.py#L62)

### `aw_review_reconcile`

Escalate a stale review or reassign an outstanding human review after departure.

- **Access** Current workshop_manager or review_lead in the review scope. A review_lead is restricted to the team they supervise.
- **Input** `review_id: string`, `operation_id: string`, `replacement?: string`
- **Result** Can keep the state, reassign a human review, or escalate it. Reassignment includes reviewer_id. `review_id: string`, `state: string`, `reviewer_id: string (reassigned)`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `EXISTING_DELEGATION_OR_DEADLINE`, `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `REVIEWER_STILL_ACTIVE`, `REVIEW_ALREADY_FINAL`, `REVIEW_DENIED`

[Registration](../reference/catalog.json#L1433) · [Request and result](../reference/examples.json#L2034) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L566)

### `aw_review_request`

Create an independent scoped review, evaluating trusted deterministic rules first.

- **Access** Current workshop_manager or review_lead in the review scope. A review_lead is restricted to the team they supervise.
- **Input** `operation_id: string`, `artifact_ref: string`, `artifact_revision: string`, `mode: SCRIPT|DIRECTION|CHARACTER|PROSE|WORLD|STABILITY|STATISTICS`, `risk_reason: SAMPLED|MATERIAL_CHANGE|DISCLOSURE|RETCON|INCIDENT|COST_DISPUTE`, `ttl_seconds: integer`, `reviewer_id?: string`, `slot_id?: string`, `max_amount?: string`, `token_limit?: integer`
- **Result** RULE execution returns RULE_PASSED or RULE_BLOCKED. HUMAN and BUDGETED_NF start PENDING; the budgeted path includes operation_id. `review_id: string`, `state: string`, `round_no: integer`, `execution: string`, `operation_id: string (BUDGETED_NF)`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `EVIDENCE_SCOPE_DENIED`, `IDEMPOTENCY_MISMATCH`, `INDEPENDENT_REVIEW_REQUIRED`, `INVALID_REVIEW`, `REVIEW_ALREADY_ASSIGNED_OR_FINAL`, `REVIEW_DENIED`, `REVIEW_SLOT_DENIED`, `RULE_REQUIRES_NO_MODEL`

[Registration](../reference/catalog.json#L1461) · [Request and result](../reference/examples.json#L2046) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L352)

### `aw_roster_get`

Read the current directory and permitted routes.

- **Access** Current organization member and employment revision; reads are scoped to the caller’s audience.
- **Input** `{}`
- **Result** The employee list and visible routes depend on the viewer and current roster revision. `org_id: string`, `revision: integer`, `preset: string`, `employees: array`, `routes: array`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L1536) · [Request and result](../reference/examples.json#L2064) · [Handler](../../workshop/src/agent_workshop/organization.py#L396)

### `aw_route_grant`

Issue a limited non-transferrable message route.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `recipient: string`, `scope_ref: string`, `sender: string`, `kind: string`, `ttl_seconds: integer`, `max_uses: integer`, `expected_revision: integer`
- **Result** Issues a temporary route and returns its grant_id and revocation_epoch. `grant_id: string`, `revocation_epoch: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_ROUTE_GRANT`, `ROUTE_DENIED`

[Registration](../reference/catalog.json#L1548) · [Request and result](../reference/examples.json#L2120) · [Handler](../../workshop/src/agent_workshop/messaging.py#L76)

### `aw_route_revoke`

Revoke a temporary route for subsequent delivery and body reads.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `grant_id: string`
- **Result** The first revocation advances revocation_epoch. Already revoked grants retain their epoch. `grant_id: string`, `revocation_epoch: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `ROUTE_DENIED`

[Registration](../reference/catalog.json#L1619) · [Request and result](../reference/examples.json#L2138) · [Handler](../../workshop/src/agent_workshop/messaging.py#L130)

### `aw_skill_freeze`

Freeze skill provenance for this employee's task.

- **Access** Current employee meeting the skill binding’s live owner, mode, scope and sharing policy checks.
- **Input** `binding_id: string`, `mode: string`, `task_ref: string`
- **Result** Requires an owned delegated task or assigned review and returns a frozen digest and epoch. `content_sha: string`, `epoch: integer`, `task_ref: string`, `skill_id: string`
- **Retry** Frozen by employment revision, task and skill. Repeating the same binding reuses the snapshot; a different binding conflicts.
- **Errors** `SKILL_DENIED`, `TASK_SCOPE_DENIED`, `TASK_SKILL_ALREADY_FROZEN`

[Registration](../reference/catalog.json#L1643) · [Request and result](../reference/examples.json#L2150) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L169)

### `aw_skill_history`

Read a frozen historical hash.

- **Access** The current employee can read a freeze for their own tenure and task, subject to the originating installation.
- **Input** `task_ref: string`, `skill_id: string`
- **Result** Returns historical=true and the frozen digest. The historical version remains readable after skill revocation. `content_sha: string`, `epoch: integer`, `historical: boolean`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `TASK_SCOPE_DENIED`

[Registration](../reference/catalog.json#L1672) · [Request and result](../reference/examples.json#L2165) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L215)

### `aw_skill_resolve`

Resolve live skill metadata without expanding NF grants.

- **Access** Current employee meeting the skill binding’s live owner, mode, scope and sharing policy checks.
- **Input** `binding_id: string`, `mode: string`
- **Result** Returns the skill, source revision, digest, mode and current epoch. `skill_id: string`, `source_revision: string`, `content_sha: string`, `mode: string`, `epoch: integer`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `SKILL_DENIED`

[Registration](../reference/catalog.json#L1696) · [Request and result](../reference/examples.json#L2178) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L127)

### `aw_skill_revoke`

Revoke live skill use.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `binding_id: string`
- **Result** Returns binding_id and the resulting epoch. Revocation advances the binding once. `binding_id: string`, `epoch: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `SKILL_DENIED`

[Registration](../reference/catalog.json#L1720) · [Request and result](../reference/examples.json#L2193) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L132)

### `aw_skill_share`

Share registered skill metadata under an explicit scope.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `skill_id: string`, `source_revision: string`, `policy: PUBLIC_SHARED|ROLE_SCOPED|TEAM_SCOPED|PRIVATE|CONTROL_ONLY`, `ttl_seconds: integer`, `target?: string`
- **Result** Returns the binding, content digest and initial epoch. `binding_id: string`, `content_sha: string`, `epoch: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `SKILL_POLICY_DENIED`

[Registration](../reference/catalog.json#L1744) · [Request and result](../reference/examples.json#L2205) · [Handler](../../workshop/src/agent_workshop/reviews_skills.py#L52)

### `aw_state_get`

Read authorized organization state, review backlog and budget.

- **Access** Current organization member and employment revision; reads are scoped to the caller’s audience.
- **Input** `{}`
- **Result** Members see a directory. Managers and authors additionally see delegation counts, reviews and a nullable budget. `org_id: string`, `revision: integer`, `preset: string`, `employees: array`, `delegations: object (management)`, `reviews: object (management)`, `budget: object|null (management)`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** common errors

[Registration](../reference/catalog.json#L1793) · [Request and result](../reference/examples.json#L2221) · [Handler](../../workshop/src/agent_workshop/manager.py#L99)

### `aw_suspend`

Suspend an employee using organization revision CAS.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `expected_revision: integer`, `employee_id: string`
- **Result** Returns organization and employment revisions after suspension. `org_id: string`, `revision: integer`, `employee_id: string`, `employment_revision: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INVALID_EMPLOYMENT_TRANSITION`, `STALE_ORG_REVISION`

[Registration](../reference/catalog.json#L1805) · [Request and result](../reference/examples.json#L2371) · [Handler](../../workshop/src/agent_workshop/organization.py#L280)

### `aw_transfer`

Transfer an employee using organization revision CAS.

- **Access** Current workshop_manager with a live employee session in the organization.
- **Input** `operation_id: string`, `expected_revision: integer`, `employee_id: string`, `role_id: string`, `supervisor_id: string`, `team_id: string`, `slot_id?: string`, `display_name?: string`
- **Result** Returns the new organization and employment revisions after a transfer. `org_id: string`, `revision: integer`, `employee_id: string`, `employment_revision: integer`
- **Retry** Retry with the original operation_id and arguments under a still-authorized session. Reusing the ID with different arguments conflicts.
- **Errors** `IDEMPOTENCY_MISMATCH`, `INCOMPATIBLE_ROLE`, `INVALID_DISPLAY_NAME`, `INVALID_EMPLOYMENT_TRANSITION`, `INVALID_SUPERVISOR`, `REASSIGN_REPORTS_FIRST`, `ROSTER_LIMIT_EXCEEDED`, `ROSTER_SCOPE_DENIED`, `SLOT_OCCUPIED`, `STALE_ORG_REVISION`, `SUPERVISOR_CYCLE`

[Registration](../reference/catalog.json#L1834) · [Request and result](../reference/examples.json#L2386) · [Handler](../../workshop/src/agent_workshop/organization.py#L280)

## Installation delegation MCP

### `nf_delegation_get`

Read the original task binding and authoritative accounting for this installation.

- **Access** The installation token, NF instance, work, branch and grant_epoch must match. The server verifies the provisioned slot and its TASK_CREATE grant.
- **Input** `protocol_version: "aw-nf.v1"`, `nf_instance_id: string`, `work_id: string`, `branch_id: string`, `operation_id: string`, `grant_epoch: integer`
- **Result** Returns the current receipt, progress and wait reason. An operation without a receipt returns DELEGATION_NOT_FOUND. Only an accepted task supplies a result UUID. Totals sum known charges and tokens. An UNKNOWN Attempt can have a null amount or token count. `protocol_version: string`, `nf_instance_id: string`, `work_id: string`, `branch_id: string`, `operation_id: string`, `task_id: string`, `receipt_ref: string`, `world_ref: object`, `generation: integer`, `task_state: string`, `accounting_state: string`, `currency: string`, `actual_amount: string`, `actual_tokens: integer`, `attempt_ids: array`, `attempts: array`, `result_ref: UUID|null`, `wait_reason: string|null`
- **Retry** Repeat the read with the same arguments; authorization is checked on every call.
- **Errors** `DELEGATION_NOT_FOUND`

[Registration](../reference/catalog.json#L2253) · [Request and result](../reference/examples.json#L2519) · [Handler](../../src/novel_factory/application/delegation.py#L286)

### `nf_delegation_submit`

Consume an NF-authorized task slot with a stable operation ID.

- **Access** The installation token, NF instance, work, branch and grant_epoch must match. The server verifies the provisioned slot and its TASK_CREATE grant.
- **Input** `protocol_version: "aw-nf.v1"`, `nf_instance_id: string`, `work_id: string`, `branch_id: string`, `operation_id: string`, `grant_epoch: integer`, `task_template_id: string`, `scope_ref: string`, `actor_binding_ref: string|null`, `role_id: string`, `generation: integer`, `budget_reservation_ref: string`, `max_amount: string`, `currency: USD|CNY`, `token_limit: integer`
- **Result** Returns current task and accounting state. accounting_state is UNKNOWN, RESERVED, SETTLED or NO_ATTEMPT. Only ACCEPTED supplies a result_ref. Only an accepted task supplies a result UUID. Totals sum known charges and tokens. An UNKNOWN Attempt can have a null amount or token count. `protocol_version: string`, `nf_instance_id: string`, `work_id: string`, `branch_id: string`, `operation_id: string`, `task_id: string`, `receipt_ref: string`, `world_ref: object`, `generation: integer`, `task_state: string`, `accounting_state: string`, `currency: string`, `actual_amount: string`, `actual_tokens: integer`, `attempt_ids: array`, `attempts: array`, `result_ref: UUID|null`, `wait_reason: string|null`
- **Retry** Serialized by installation and operation_id. The same request returns current task/accounting state, while changed input conflicts. A recovery fence blocks new bindings.
- **Errors** `BUDGET_EXCEEDED`, `BUDGET_MISMATCH`, `DELEGATION_RECOVERY_FENCED`, `IDEMPOTENCY_MISMATCH`, `SLOT_CONSUMED`

[Registration](../reference/catalog.json#L2295) · [Request and result](../reference/examples.json#L2555) · [Handler](../../src/novel_factory/application/delegation.py#L286)

## HTTP routes

### `GET /api/v1/tasks/{task_id}`

Read current task state and generation.

- **Access** Task owner, READ
- **Input** `{task_id}: UUID` (path)
- **Result** Lease, context, result and Attempt references are nullable across task states. `attempt_id: string`, `branch_id: string`, `context_bundle_id: string`, `created_at: string`, `current_result_id: null`, `deadline: string`, `dependency_fingerprint: string`, `generation: integer`, `lease_expiry: string`, `role_id: string`, `state: string`, `task_id: string`, `task_kind: string`, `updated_at: string`, `work_id: string`, `world_ref: object`
- **Retry** Read again to obtain the current state.
- **Errors** common errors

[Registration](../reference/catalog.json#L2) · [Request and result](../reference/examples.json#L2) · [Handler](../../src/novel_factory/application/kernel.py#L513)

### `GET /health`

Check process liveness.

- **Access** Public
- **Input** `{}`
- **Result** `status: string`, `release: string`
- **Retry** Read again to obtain the current state.
- **Errors** common errors

[Registration](../reference/catalog.json#L15) · [Request and result](../reference/examples.json#L32) · [Handler](../../src/novel_factory/interfaces/http.py#L288)

### `POST /api/v1/branches`

Fork a fixed revision.

- **Access** BRANCH, or REPAIR for a repair branch
- **Input** `base_world_ref: WorldRef`, `kind: CANDIDATE|SIMULATION|REPAIR`, `idempotency_key: string`
- **Result** `work_id: string`, `branch_id: string`, `world_revision_id: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `INVALID_ARGUMENT`

[Registration](../reference/catalog.json#L22) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L41) · [Handler](../../src/novel_factory/application/kernel.py#L936)

### `POST /api/v1/contexts/compile`

Compile and bind a context to a task.

- **Access** COMPILE, authorized recipe and knowledge cut
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `recipe_id: string`, `input_manifest_ref: ArtifactRef`, `read_contract: ReadContract`, `knowledge_cut: KnowledgeCut`, `subject_time: SubjectTime`, `token_budget: integer`, `idempotency_key: string`
- **Result** `schema_version: string`, `status: string`, `compiled: object`, `compiled_artifact_ref: object`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `CAPACITY_LIMIT`, `CONTEXT_INCOMPLETE`, `IDEMPOTENCY_CONFLICT`, `INVALID_ARGUMENT`, `TASK_NOT_ACCEPTABLE`, `TASK_REBIND_REQUIRED`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L32) · [JSON Schema](../../contracts/temporal_concurrency.schema.json) · [Request and result](../reference/examples.json#L59) · [Handler](../../src/novel_factory/application/queries.py#L580)

### `POST /api/v1/continuity/prepare`

Prepare a continuity candidate.

- **Access** Non-actor controller with WORLD, PLAN and ARTIFACT_WRITE
- **Input** `schema_version: "2.3"`, `series_id: UUID`, `operation: APPEND|REVISE`, `world_ref: WorldRef`, `expected_prior_ref: ArtifactRef|null`, `ordered_release_refs: array`, `entries: array`, `revision_reason: string|null`, `idempotency_key: string`
- **Result** `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `CAPACITY_LIMIT`

[Registration](../reference/catalog.json#L42) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L204) · [Handler](../../src/novel_factory/application/continuity.py#L199)

### `POST /api/v1/model-routes/select`

Select a server-configured adapter.

- **Access** WORLD ADMIN, installed adapter and expected selection
- **Input** `schema_version: "2.1"`, `world_ref: WorldRef`, `idempotency_key: string`, `adapter_id: string`, `expected_ref: ArtifactRef|null`
- **Result** `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `HEAD_CONFLICT`, `MODEL_CONFIG_CHANGED`, `MODEL_UNAVAILABLE`

[Registration](../reference/catalog.json#L52) · [JSON Schema](../../contracts/upgrades.schema.json) · [Request and result](../reference/examples.json#L234) · [Handler](../../src/novel_factory/application/upgrades.py#L214)

### `POST /api/v1/proposals`

Build a proposal from a registered template.

- **Access** Bound task with PROPOSE; template requires APPROVE, repair also needs REPAIR
- **Input** `task_id: UUID`, `generation: integer`, `idempotency_key: string`, `command: TemplateCommand`
- **Result** `proposal_ref: object`, `approval_id: string`, `base_world_ref: object`, `context_bundle_id: string`, `dependency_fingerprint: string`, `dependency_handle: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `HARD_RULE_VIOLATION`, `HISTORY_REPAIR_REQUIRED`, `INVALID_ARGUMENT`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L62) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L254) · [Handler](../../src/novel_factory/application/kernel.py#L556)

### `POST /api/v1/queries/batch`

Run queries at one shared cut.

- **Access** Task READ and per-item plane and operation permission
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `read_contract: ReadContract`, `knowledge_cut: KnowledgeCut`, `items: array`
- **Result** `schema_version: string`, `status: string`, `request_id: string`, `read_cut: object`, `items: array`, `all_items_succeeded: boolean`
- **Retry** Repeat with the same parameters. Retain the resolved cut and cursor across pages. Successful reads can record dependencies.
- **Errors** `CAPACITY_LIMIT`, `INVALID_ARGUMENT`

[Registration](../reference/catalog.json#L72) · [JSON Schema](../../contracts/temporal_concurrency.schema.json) · [Request and result](../reference/examples.json#L292) · [Handler](../../src/novel_factory/application/queries.py#L552)

### `POST /api/v1/queries/semantic-assumptions`

Attach an assumption to an existing read receipt.

- **Access** Task READ on the matching generation and dependency handle
- **Input** `task_id: UUID`, `generation: integer`, `dependency_handle: UUID`, `dependency_kind: SOURCE_OF|DERIVED_FROM|BELIEVES|ASSUMES_TRUE|REQUIRES|EVIDENCE_FOR|CONTRADICTS|RESOURCE_DEPENDENCY|TEMPORAL_DEPENDENCY|COMMUNICATION_DEPENDENCY`, `statement: string`
- **Result** `observation_id: string`, `proposition_key: string`, `uncertain: boolean`
- **Retry** Reuse the original read handle and assumption. The read receipt scopes supplemental evidence.
- **Errors** `TASK_NOT_ACCEPTABLE`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L82) · [JSON Schema](../../contracts/kernel.schema.json) · [Request and result](../reference/examples.json#L469) · [Handler](../../src/novel_factory/application/semantic_dependencies.py#L387)

### `POST /api/v1/queries/timeline`

Query a timeline, state, plans or graph at a resolved cut.

- **Access** READ on the bound generation and requested plane
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `read_contract: ReadContract`, `knowledge_cut: KnowledgeCut`, `subject_time: SubjectTime`, `query: QueryOperation`, `page_size: integer`, `cursor: string|null`
- **Result** `schema_version: string`, `status: string`, `request_id: string`, `read_cut: object`, `payload: object`, `freshness: string`, `dependency_handle: string`, `consistency_status: string`
- **Retry** Repeat with the same parameters. Retain the resolved cut and cursor across pages. Successful reads can record dependencies.
- **Errors** `CAPACITY_LIMIT`

[Registration](../reference/catalog.json#L92) · [JSON Schema](../../contracts/temporal_concurrency.schema.json) · [Request and result](../reference/examples.json#L485) · [Handler](../../src/novel_factory/application/queries.py#L519)

### `POST /api/v1/refinements/prepare`

Prepare evidence of equivalent refinement.

- **Access** REPAIR on WORLD, non-actor identity within its time horizon
- **Input** `schema_version: "2.3"`, `source_world_ref: WorldRef`, `macro_event_ref: EventRef`, `steps: array`, `idempotency_key: string`
- **Result** `proof_ref: object`, `replacement_event_ref: object`, `repair_command: object`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `INVALID_EVIDENCE`

[Registration](../reference/catalog.json#L102) · [JSON Schema](../../contracts/refinement.schema.json) · [Request and result](../reference/examples.json#L618) · [Handler](../../src/novel_factory/application/refinement.py#L79)

### `POST /api/v1/refinements/proposals`

Propose a previously proved refinement.

- **Access** Authorized repair-branch task, proof and proposal permissions
- **Input** `task_id: UUID`, `generation: integer`, `proof_ref: ArtifactRef`, `idempotency_key: string`
- **Result** `proposal_ref: object`, `approval_id: null`, `base_world_ref: object`, `context_bundle_id: string`, `dependency_fingerprint: string`, `dependency_handle: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** common errors

[Registration](../reference/catalog.json#L112) · [JSON Schema](../../contracts/refinement.schema.json) · [Request and result](../reference/examples.json#L787) · [Handler](../../src/novel_factory/application/refinement.py#L161)

### `POST /api/v1/runtime-upgrades/activate`

Activate a reviewed interpretation version.

- **Access** WORLD ADMIN and currently valid review receipt
- **Input** `schema_version: "2.1"`, `world_ref: WorldRef`, `idempotency_key: string`, `package_ref: ArtifactRef`, `approval_ref: ArtifactRef`
- **Result** `old_world_ref: object`, `world_ref: object`, `package_ref: object`, `approval_ref: object`, `new_world_effects: integer`, `interpretation_epoch: integer`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `APPROVAL_STALE`, `HEAD_CONFLICT`, `PROJECTION_CORRUPT`

[Registration](../reference/catalog.json#L122) · [JSON Schema](../../contracts/upgrades.schema.json) · [Request and result](../reference/examples.json#L817) · [Handler](../../src/novel_factory/application/upgrades.py#L140)

### `POST /api/v1/runtime-upgrades/prepare`

Prepare an immutable runtime upgrade package.

- **Access** Non-actor WORLD ADMIN covering the branch horizon
- **Input** `schema_version: "2.1"`, `world_ref: WorldRef`, `idempotency_key: string`, `rules: object`, `interpretation: object`
- **Result** `package_ref: object`, `rules_ref: object`, `interpretation_ref: object`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `HEAD_CONFLICT`, `INVALID_ARGUMENT`, `PROJECTION_CORRUPT`, `UPGRADE_REQUIRES_REPAIR`

[Registration](../reference/catalog.json#L132) · [JSON Schema](../../contracts/upgrades.schema.json) · [Request and result](../reference/examples.json#L864) · [Handler](../../src/novel_factory/application/upgrades.py#L56)

### `POST /api/v1/runtime-upgrades/reviews`

Review an upgrade package.

- **Access** Independent WORLD reviewer with APPROVE
- **Input** `schema_version: "2.1"`, `world_ref: WorldRef`, `idempotency_key: string`, `package_ref: ArtifactRef`, `decision: "APPROVE"`, `notes: string`
- **Result** `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `INDEPENDENT_REVIEW_REQUIRED`

[Registration](../reference/catalog.json#L142) · [JSON Schema](../../contracts/upgrades.schema.json) · [Request and result](../reference/examples.json#L915) · [Handler](../../src/novel_factory/application/upgrades.py#L120)

### `POST /api/v1/tasks`

Freeze the input specification and create a task.

- **Access** TASK_CREATE with matching role and grant scope
- **Input** `spec: TaskSpec`, `idempotency_key: string`
- **Result** `task_id: string`, `generation: integer`, `state: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** common errors

[Registration](../reference/catalog.json#L152) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L940) · [Handler](../../src/novel_factory/application/kernel.py#L466)

### `POST /api/v1/tasks/cancel`

Cancel a task generation.

- **Access** Task owner and READ
- **Input** `task_id: UUID`, `generation: integer`, `idempotency_key: string`
- **Result** `task_id: string`, `generation: integer`, `receipt_id: string`, `status: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** common errors

[Registration](../reference/catalog.json#L162) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L987) · [Handler](../../src/novel_factory/application/kernel.py#L519)

### `POST /api/v1/tasks/claim`

Claim a lease on ready work.

- **Access** TASK_CLAIM for the authorized work
- **Input** `worker_id: string`, `lease_seconds?: integer = 30`, `task_id?: UUID`
- **Result** An empty queue returns HTTP 200 with JSON `null`. A successful claim returns `task_id: string`, `work_id: string`, `branch_id: string`, `principal_id: string`, `grant_id: string`, `task_kind: string`, `role_id: string`, `state: string`, `generation: integer`, `context_bundle_id: null`, `dependency_fingerprint: null`, `lease_owner: string`, `lease_expiry: string`, `deadline: string`, `current_result_id: null`, `attempt_id: string`, `created_at: string`, `updated_at: string`, `world_ref: object`
- **Retry** A claim changes the lease. Inspect the task and worker after an uncertain reply before claiming more work.
- **Errors** `VIEW_DENIED`

[Registration](../reference/catalog.json#L172) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L1002) · [Handler](../../src/novel_factory/application/kernel.py#L496)

### `POST /api/v1/tasks/results`

Store a worker result for the specified generation.

- **Access** Task owner and matching worker lease
- **Input** `result: TaskResult`, `worker_id: string`
- **Result** `task_id: string`, `generation: integer`, `receipt_id: string`, `status: string`
- **Retry** Resubmit the same result identity, generation and contents. Inspect the task after a content or lease conflict.
- **Errors** `ATTEMPT_MISMATCH`, `CONTEXT_MISMATCH`, `IDEMPOTENCY_CONFLICT`, `OBJECT_STORE_REQUIRED`, `TASK_NOT_ACCEPTABLE`

[Registration](../reference/catalog.json#L182) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L1036) · [Handler](../../src/novel_factory/orchestration/tasks.py#L285)

### `POST /api/v1/tasks/{task_id}/continuity`

Deliver authorized continuity to a task generation, with a default budget of 100,000 bytes.

- **Access** Current task identity with the snapshot in its authorized inputs
- **Input** `{task_id}: UUID` (path), `schema_version: "2.3"`, `generation: integer`, `snapshot_ref: ArtifactRef`, `budget_bytes?: integer`
- **Result** `authority: string`, `entries: array`, `snapshot_ref: object`, `world_effects_applied: integer`, `world_ref: object`
- **Retry** Read again to obtain the current state.
- **Errors** `CONTEXT_BUDGET_EXCEEDED`, `CONTINUITY_REVALIDATION_REQUIRED`, `INVALID_ARGUMENT`, `TASK_REBIND_REQUIRED`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L192) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L1061) · [Handler](../../src/novel_factory/application/continuity.py#L441)

### `POST /api/v1/works/{work_id}/continuity/inspect`

Inspect a snapshot against its target world.

- **Access** Non-actor READ with WORLD and PLAN
- **Input** `{work_id}: UUID` (path), `schema_version: "2.3"`, `snapshot_ref: ArtifactRef`, `target_world_ref?: WorldRef|null`
- **Result** `current_impact: object`, `qualification: string`, `snapshot: object`, `snapshot_ref: object`, `world_effects_applied: integer`
- **Retry** Read again to obtain the current state.
- **Errors** `VIEW_DENIED`

[Registration](../reference/catalog.json#L208) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L1139) · [Handler](../../src/novel_factory/application/continuity.py#L396)

### `POST /api/v1/works/{work_id}/continuity/publish`

Select a reviewed continuity snapshot.

- **Access** Controller with APPROVE and a currently qualified reviewer
- **Input** `{work_id}: UUID` (path), `schema_version: "2.3"`, `candidate_ref: ArtifactRef`, `review_ref: ArtifactRef`, `idempotency_key: string`
- **Result** `external_delivery: string`, `selection_epoch: integer`, `snapshot_ref: object`, `world_effects_applied: integer`, `world_ref: object`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `APPROVAL_REQUIRED`, `APPROVAL_REVOKED`, `CONTINUITY_REVALIDATION_REQUIRED`

[Registration](../reference/catalog.json#L224) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L1243) · [Handler](../../src/novel_factory/application/continuity.py#L316)

### `POST /api/v1/works/{work_id}/continuity/readings`

Read a candidate and record its reading receipt.

- **Access** Independent reader with WORLD, PLAN and APPROVE on current revision
- **Input** `{work_id}: UUID` (path), `schema_version: "2.3"`, `candidate_ref: ArtifactRef`, `idempotency_key: string`
- **Result** `candidate: object`, `comprehension: string`, `original_texts: array`, `reading_ref: object`, `world_effects_applied: integer`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `CAPACITY_LIMIT`, `CONTINUITY_REVALIDATION_REQUIRED`

[Registration](../reference/catalog.json#L240) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L1279) · [Handler](../../src/novel_factory/application/continuity.py#L220)

### `POST /api/v1/works/{work_id}/continuity/reviews`

Record a continuity review.

- **Access** WORLD, PLAN, APPROVE and the matching reading receipt
- **Input** `{work_id}: UUID` (path), `schema_version: "2.3"`, `candidate_ref: ArtifactRef`, `reading_ref: ArtifactRef`, `decision: APPROVED|CHANGES_REQUIRED`, `reason: string`, `acknowledged_affected_ordinals: array`, `idempotency_key: string`
- **Result** `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `CONTINUITY_IMPACT_UNREVIEWED`, `CONTINUITY_REVALIDATION_REQUIRED`, `INVALID_READING_COVERAGE`, `SELF_APPROVAL_DENIED`, `STALE_CONTINUITY_WORLD`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L256) · [JSON Schema](../../contracts/continuity.schema.json) · [Request and result](../reference/examples.json#L1377) · [Handler](../../src/novel_factory/application/continuity.py#L265)

### `POST /api/v1/world/accept`

Accept a proposal against its expected head.

- **Access** READ and ACCEPT for a new write, with valid approvals and a current generation. A committed receipt may be replayed with READ.
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `context_bundle_id: UUID`, `expected_world_ref: WorldRef`, `proposal_ref: ArtifactRef`, `approval_refs: array`, `dependency_fingerprint: Sha256`, `idempotency_key: string`
- **Result** `command_id: string`, `world_ref: object`, `branch_seq: integer`, `accepted_event_refs: array`, `manifest_sha256: string`, `recorded_at: string`
- **Retry** Replay the same key and command with READ to retrieve the committed receipt. A first execution requires ACCEPT. Inspect the current revision after a conflict.
- **Errors** `ALREADY_ACCEPTED`, `APPROVAL_STALE`, `BRANCH_CHANGED`, `CANON_MOVED`, `DEPENDENCY_CONFLICT`, `HEAD_CONFLICT`, `HISTORY_CHANGED`, `INVALID_ARGUMENT`, `SCHEMA_UNSUPPORTED`, `STALE_CERTIFICATE`, `TASK_REBIND_REQUIRED`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L272) · [JSON Schema](../../contracts/core.schema.json) · [Request and result](../reference/examples.json#L1406) · [Handler](../../src/novel_factory/application/kernel.py#L805)

### `POST /api/v1/world/accept-guarded`

Accept a proposal using its guarded certificate.

- **Access** READ and ACCEPT for a new write, with valid approvals and a current generation. A committed receipt may be replayed with READ.
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `context_bundle_id: UUID`, `concurrency_mode: string`, `base_world_ref: WorldRef`, `proposal_ref: ArtifactRef`, `acceptance_certificate_ref: ArtifactRef`, `approval_refs: array`, `dependency_fingerprint: Sha256`, `idempotency_key: string`
- **Result** `command_id: string`, `world_ref: object`, `branch_seq: integer`, `accepted_event_refs: array`, `manifest_sha256: string`, `recorded_at: string`, `original_base_world_ref: object`, `validation_world_ref: object`, `parent_world_ref: object`, `acceptance_certificate_ref: object`, `proposal_ref: object`
- **Retry** Replay the same key and command with READ to retrieve the committed receipt. A first execution requires ACCEPT. Inspect the current revision after a conflict.
- **Errors** `ALREADY_ACCEPTED`, `APPROVAL_STALE`, `BRANCH_CHANGED`, `CANON_MOVED`, `DEPENDENCY_CONFLICT`, `HEAD_CONFLICT`, `HISTORY_CHANGED`, `INVALID_ARGUMENT`, `STALE_CERTIFICATE`, `TASK_REBIND_REQUIRED`, `UNPROVEN_FOOTPRINT`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L282) · [JSON Schema](../../contracts/temporal_concurrency.schema.json) · [Request and result](../reference/examples.json#L1448) · [Handler](../../src/novel_factory/application/kernel.py#L805)

### `POST /api/v1/world/acceptance-certificates`

Prepare an acceptance certificate.

- **Access** ACCEPT, bound task context and approvals
- **Input** `schema_version: string`, `task_id: UUID`, `generation: integer`, `context_bundle_id: UUID`, `concurrency_mode: string`, `base_world_ref: WorldRef`, `proposal_ref: ArtifactRef`, `approval_refs: array`, `dependency_handle: UUID`, `idempotency_key: string`
- **Result** `artifact_id: string`, `artifact_revision_id: string`, `sha256: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `BRANCH_CHANGED`, `DEPENDENCY_CONFLICT`, `HARD_RULE_VIOLATION`, `HEAD_CONFLICT`, `HISTORY_CHANGED`, `INVALID_ARGUMENT`, `STALE_CERTIFICATE`, `UNPROVEN_FOOTPRINT`

[Registration](../reference/catalog.json#L292) · [JSON Schema](../../contracts/temporal_concurrency.schema.json) · [Request and result](../reference/examples.json#L1521) · [Handler](../../src/novel_factory/application/kernel.py#L705)

### `POST /api/v1/world/promotions`

Promote a reviewed REPAIR head to canonical.

- **Access** BRANCH on both branches and PROMOTE on the repair. Requires a prepared head, independently approved repair and the matching canonical base.
- **Input** `old_world_ref: WorldRef`, `candidate_world_ref: WorldRef`, `idempotency_key: string`
- **Result** `work_id: string`, `branch_id: string`, `world_revision_id: string`
- **Retry** Retry a lost reply with the same key and command. Read current state after a version or qualification conflict.
- **Errors** `HEAD_CONFLICT`, `REPAIR_INCOMPLETE`, `VIEW_DENIED`

[Registration](../reference/catalog.json#L302) · [JSON Schema](../../contracts/http.schema.json) · [Request and result](../reference/examples.json#L1552) · [Handler](../../src/novel_factory/application/kernel.py#L970)
