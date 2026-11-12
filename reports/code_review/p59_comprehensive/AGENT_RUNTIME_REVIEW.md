# Agent Runtime review

Reviewed commit `5e322e8af8ddb4465f68f8cab84c9685832461a9`. This reviewer read and exercised the existing code before any product edits. The baseline suites used fixture credentials and local service endpoints.

| Decision | Authoritative path | Evidence and conclusion |
|---|---|---|
| Install a provider | `agent_runtime/providers.py:44–129`, `src/novel_factory/infrastructure/runtime_providers.py:6–32` | Three concrete transports share the operator profile. OpenAI-compatible and DeepSeek construct different output-bound fields. A model does not supply an endpoint, credential name, budget or transport. Catalog finding P59-RT-01 concerns invalid operator input. |
| Select a route | `agent_runtime/routing.py:8–73`, `src/novel_factory/application/model_routing.py:15–75`, `workshop/src/agent_workshop/model_policies.py:29–55` | Eleven selector levels including default. Overrides occur in privileged callers. Policy scope, revision, matched rule and configuration hash are bound in NF. Duplicate selectors are rejected. `test_resolution_precedence_and_ambiguous_rejection` exercises the order and override. No dynamic upstream capability discovery was found. |
| Change the route | `src/novel_factory/application/model_routing.py:83–163` | Locks the task, validates dependencies, retains prior generation and creates a new immutable input. Only confirmed unsent prepared attempts can be released. A sent/terminal or unresolved call requires reconciliation. Generation-bound Workshop and creative assignments require fresh enrollment. |
| Send and reconcile | `src/novel_factory/application/agent_gateway.py:523–545,573–689`, `src/novel_factory/application/supervisor.py:26–76` | Distinct task/gen/call key, budget reserve, host check and single upstream send. Protocol/availability failures and missing bills have different consequences. Token counts, reserved funds and settled supplier amounts have separate meanings. Local refusal before reservation returns NOT_SENT. After marking SENT, a provider failure is accounted conservatively; an UNKNOWN cannot be transparently retried. |
| Limit execution | `migrations/versions/0021_p58_admission.py:45–69`, `src/novel_factory/orchestration/runtime_admission.py:26–50`, `src/novel_factory/orchestration/dispatch.py:8–54` | A PG send gate serializes the final capacity decision. Limits intersect across provider, account, work, role and organization. Virtual finish and queue age govern claim choice. A timed-out supervisor must let the bounded lease/transport expire. HTTP rate-limit counts cannot be interpreted as DB wait samples. |
| Isolate providers | `agent_runtime/providers.py:132–201`, `agent_runtime/codex.py:140–215` | One isolated HTTP helper per outbound call; the Codex adapter controls the native turn and guards upstream handoff. One-profile failure with retained UNKNOWN allowed a subsequent new task on a second profile while preserving the original reservation; separate-account live availability remains unmeasured. |

## Executed baseline

`p58-focused.xml` contains 33 successful tests and zero skips in 30.01 s. The group includes model routing, provider evidence, pipeline, P5.8 readiness regressions and unit checks of provider formats, rejection, timeout and scoped policy. Cluster PostgreSQL 17.11 used checksums, fsync and synchronous commit. The native host/DeepSeek overlap case ran against synthetic upstream replies. No paid provider request or receipt was obtained; literary quality was not evaluated.

## Performance observation

With 256 rules, 256 calls under cProfile attributed 0.758/0.994 cumulative seconds to `validate_rules`. Each rule reconstructs selector sets for the same 11 allowed levels before matching. P59-RT-02 proposes reusing the static selector grammar and its priority table; current policy, permissions and hashes must continue to be checked on every call. Measurements cover pure resolution only. CPU profiles and per-call samples accompany the performance review.

## Limits of independent review

This review follows selected public execution paths and concrete fixture cases. The final database, recovery, contention and four-project experiments belong to the engineering acceptance record. The process-local host binary was available; this reviewer did not sign into an upstream account or assess a commercial model.
