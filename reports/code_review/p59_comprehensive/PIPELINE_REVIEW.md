# Multi-work and cross-volume pipeline

Reviewed the actual production board and supervision paths at `5e322e8af8ddb4465f68f8cab84c9685832461a9`.

* Creation verifies the branch HEAD, budget ownership, plan revision and artifacts under an ADMIN grant. A board accepts up to 128 nodes and two future volume ordinals. An unfinished board or an unresolved financial attempt on a previous current or historical node blocks replacement (`src/novel_factory/application/production_pipeline.py:152–215`).
* Picking a stage implements three WIP limits, chapter-readiness checks and total review backpressure. Each transaction advances at most one node. Enrollment creates a native NF task and persists the expected route, budget, generation-bound material, trace and role (`production_pipeline.py:248–323`). Dispatch and final delivery recheck the original owner's authorization and deadline.
* Writers bind accepted predecessor handoffs. A chapter reader binds its exact source drafts and has different grant and principal from every writer. The resulting review is advisory. The forward-looking PLAN candidate retains dependencies, assumptions and its original frozen input. Moving a window into native planning requires explicit authorization and an up-to-date published predecessor (`production_pipeline.py:338–393,397–476`).
* Independent PG tests verified two projects with separate budgets and shared provider limit, local failure remaining confined to the original Attempt, overlapping draft/plan dispatch, early review, local replan, immutable old candidates and failure to adopt at expiry. `p58-focused.xml` contains the exercise in the wider 33-test group.

## P59-PIPE-01 — earlier revision's attempt missing from status

P2, reproduced via a successful, ordinary planning revision on PG17.11. `pipeline_status` at lines 225–229 collects task IDs only from current nodes, then loops through the same attempt rows once per node. `revise_window` retains the earlier task in the node's `history` at lines 492–499. The status exporter returns that task but omits its attempt ID. The immutable attempts table and accounting entries remain intact. The operator needs an extra query to associate one of seven calls with a run after a single revision.

Evidence: baseline fixture `test_native_parallel_drafts_conditional_plans_and_early_review`, seven local HTTP requests, `pipeline-ledger.json`. The superseded task has a ledger attempt; compare that task's record under `after_local_revision.*.history` with `nf_attempts`. Revisions allow up to two history records per candidate.

Remedy: collect current plus historical task IDs, query the scoped attempts once, group by task, and attach IDs to their owning record. Retain current node semantics and do not aggregate old attempts into the current generation. Add a PG regression assertion reconciling both IDs from the routine operator status export. No schema or ledger change is needed.
