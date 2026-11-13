"""Argument contracts for tools on a server-provisioned NF binding."""
from jsonschema import Draft202012Validator, FormatChecker

from novel_factory.domain.creative_review import RUBRICS
from .prompt_schema import inline_schema as _inline_schema


def object_schema(*, optional=(), **properties):
    return {"type": "object", "properties": properties, "required": [key for key in properties if key not in optional],
            "additionalProperties": False}


_LOOKUP = object_schema(subject_id={"type": "string", "format": "uuid"},
                        predicate={"type": "string", "minLength": 1, "maxLength": 128})
_UUID = {"type": "string", "format": "uuid"}
_ARTIFACT = object_schema(artifact_id=_UUID, artifact_revision_id=_UUID,
                          sha256={"type": "string", "pattern": "^[a-f0-9]{64}$"})
_EVENT = object_schema(occurrence_id=_UUID, event_version_id=_UUID)
_REASON = {"type": "string", "minLength": 1, "maxLength": 10000}
_KEY = {"type": "string", "minLength": 1, "maxLength": 96}
_SPAN_OFFSET = {"type": "integer", "minimum": 0}
_REVIEW_OBSERVATION = object_schema(
    optional=("severity", "disposition", "speakers"),
    pattern={"enum": sorted({pattern for layer in RUBRICS.values() for pattern in layer})},
    start=_SPAN_OFFSET, end=_SPAN_OFFSET,
    reason={"type": "string", "minLength": 1, "maxLength": 2000},
    counterinterpretation={"type": "string", "minLength": 1, "maxLength": 2000},
    narrative_function={"type": "string", "minLength": 1, "maxLength": 1000},
    requested_change={"anyOf": [{"type": "string", "minLength": 1, "maxLength": 1000}, {"type": "null"}]},
    related_spans={"type": "array", "maxItems": 32,
                   "items": object_schema(text_ref=_ARTIFACT, start=_SPAN_OFFSET, end=_SPAN_OFFSET)},
    speakers={"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string", "minLength": 1}},
    severity={"enum": ["NOTE", "MINOR", "MAJOR", "BLOCKER"]},
    disposition={"enum": ["QUESTION", "PREFERENCE", "PRESERVE"]},
)

_WORLD = _inline_schema("WorldRef", "2.0")
_EPOCH = {"type": "integer", "minimum": 0}
_INSTRUCTION = {"type": "string", "minLength": 1, "maxLength": 200000}
_GENERATION = {"type": "integer", "minimum": 1}
_UNTIL = {"type": "string", "format": "date-time"}
_REVIEW_REFS = {"type": "array", "maxItems": 32, "uniqueItems": True, "items": _ARTIFACT}
_EVALUATION_REFS = {"candidate_ref": _ARTIFACT, "engineering_report_ref": _ARTIFACT, "run_manifest_ref": _ARTIFACT}
_VERDICT = {"enum": ["APPROVE", "REJECT"]}
_NAME = {"type": "string", "minLength": 1, "maxLength": 128}
_TOOLS = {
    "nf_harness_metrics": ("Read measured creative workflow activity in the bound work, branch and time window.",
        object_schema(optional=("since", "until", "limit"), world_ref=_WORLD, since=_UNTIL, until=_UNTIL,
                      limit={"type": "integer", "minimum": 1, "maximum": 5000})),
    "nf_harness_instruction_propose": ("Freeze a scoped instruction and comparison using signed manuscript reviews.",
        object_schema(optional=("instructions", "material_order", "evaluation_report_refs"), world_ref=_WORLD,
                      task_id=_UUID, generation=_GENERATION, failure_report_refs={**_REVIEW_REFS, "minItems": 1},
                      success_report_refs=_REVIEW_REFS, evaluation_report_refs={**_REVIEW_REFS, "minItems": 1},
                      change_kind={"enum": ["ROLE_INSTRUCTIONS", "SKILL_INSTRUCTIONS", "CONTEXT_RECIPE", "REVIEW_RUBRIC"]},
                      quota=_inline_schema("Quota", "harness"), order_seed={"type": "integer", "minimum": 0,
                      "maximum": 2147483647}, repetitions={"type": "integer", "minimum": 1, "maximum": 32},
                      instructions={"type": "string", "minLength": 1, "maxLength": 16000},
                      material_order={"type": "array", "minItems": 1, "maxItems": 11, "uniqueItems": True, "items": _NAME},
                      idempotency_key=_KEY)),
    "nf_harness_instruction_evaluate": ("Run every preregistered arm and retain outcomes for independent review.",
        object_schema(world_ref=_WORLD, candidate_ref=_ARTIFACT, idempotency_key=_KEY)),
    "nf_harness_instruction_case": ("Describe the exact participants and artifacts for an independent registrar.",
        object_schema(world_ref=_WORLD, **_EVALUATION_REFS, reviewer_task_id=_UUID, reviewer_generation=_GENERATION,
                      publisher_grant_id=_UUID)),
    "nf_harness_instruction_evidence": ("Read the assigned evaluation outcomes and scope.",
        object_schema(**_EVALUATION_REFS)),
    "nf_harness_instruction_review": ("Record an independent opinion on the assigned experiment.",
        object_schema(**_EVALUATION_REFS, publisher_grant_id=_UUID, verdict=_VERDICT, reason=_REASON,
                      valid_until=_UNTIL, idempotency_key=_KEY)),
    "nf_harness_instruction_decide": ("Decide whether to release independently reviewed instructions.",
        object_schema(optional=("route", "policy_ref"), **_EVALUATION_REFS, world_ref=_WORLD, review_ref=_ARTIFACT,
                      action=_VERDICT, expected_epoch=_EPOCH, reason=_REASON, valid_until=_UNTIL, idempotency_key=_KEY,
                      route={"enum": ["MANUAL", "POLICY_AUTOMATIC"]}, policy_ref=_ARTIFACT)),
    "nf_harness_instruction_policy": ("Authorize a scoped publisher under an expiring automatic promotion policy.",
        object_schema(optional=("dynamic_instance_ref",), world_ref=_WORLD, role_id=_NAME, task_kind=_NAME, skill_name=_NAME,
                      dynamic_instance_ref=_ARTIFACT, publisher_grant_id=_UUID, valid_until=_UNTIL, idempotency_key=_KEY)),
    "nf_harness_instruction_revoke_policy": ("Withdraw the scoped authority for subsequent automatic releases.",
        object_schema(world_ref=_WORLD, policy_ref=_ARTIFACT, reason=_REASON, idempotency_key=_KEY)),
    "nf_harness_instruction_monitor": ("Read NF attempts and settlement for a task that froze a specific release.",
        object_schema(world_ref=_WORLD, decision_ref=_ARTIFACT, task_id=_UUID, generation=_GENERATION, idempotency_key=_KEY)),
    "nf_harness_instruction_rollback": ("Restore the previous scoped selection at the expected release epoch.",
        object_schema(optional=("review_ref",), world_ref=_WORLD, decision_ref=_ARTIFACT, expected_epoch=_EPOCH,
                      reason=_REASON, review_ref=_ARTIFACT, idempotency_key=_KEY)),
    "nf_harness_agent_register": ("Register scoped profile and Skill text from a delegated source assignment.",
        object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1},
                      workshop_binding=_inline_schema("WorkshopBinding", "harness"),
                      profile_version={"type": "string", "minLength": 1, "maxLength": 80},
                      role_instructions=_INSTRUCTION, skill_instructions=_INSTRUCTION,
                      requested_tools={"type": "array", "maxItems": 64, "uniqueItems": True,
                                       "items": {"type": "string", "minLength": 1, "maxLength": 100}},
                      trigger={"enum": ["SKILL_GAP", "REVIEW_FINDING", "TASK_DEMAND"]},
                      evidence_refs={"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                                     "items": _ARTIFACT}, quota=_inline_schema("Quota", "harness"),
                      idempotency_key=_KEY)),
    "nf_harness_agent_select": ("Select a registered revision for new assignments, including a historical revision.",
        object_schema(world_ref=_WORLD, instance_ref=_ARTIFACT, revision_ref=_ARTIFACT,
                      expected_epoch=_EPOCH, idempotency_key=_KEY)),
    "nf_harness_agent_retire": ("Exit or archive the scoped dynamic agent at its expected version.",
        object_schema(world_ref=_WORLD, instance_ref=_ARTIFACT, action={"enum": ["EXIT", "ARCHIVE"]},
                      expected_epoch=_EPOCH, idempotency_key=_KEY)),
    "nf_harness_agent_history": ("Read the dynamic agent's authorized selection and lifecycle history.",
        object_schema(world_ref=_WORLD, instance_ref=_ARTIFACT)),
    "nf_harness_review_independence": ("Verify the named review assignment and manuscript against trusted subject records.",
        object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1}, manuscript_ref=_ARTIFACT)),
    "nf_harness_review_read": ("Read the exact assigned manuscript and obtain a coverage receipt.",
        object_schema(draft_ref=_ARTIFACT, idempotency_key=_KEY)),
    "nf_harness_review_submit": ("Report precisely located literary findings for the assigned manuscript.",
        object_schema(optional=("prior_report_ref", "related_independence_refs", "reader_position"), binding=_inline_schema("EditorialBinding", "editorial"),
                      target_ref=_ARTIFACT, independence_ref=_ARTIFACT,
                      related_independence_refs={"type": "array", "maxItems": 127, "items": _ARTIFACT},
                      scope_draft_refs={"type": "array", "minItems": 1, "maxItems": 128, "items": _ARTIFACT},
                      coverage_refs={"type": "array", "minItems": 1, "maxItems": 512, "items": _ARTIFACT},
                      layer={"enum": list(RUBRICS)},
                      reader_position={"enum": ["NO_OPINION", "NO_ISSUES"]},
                      observations={"type": "array", "maxItems": 64, "items": _REVIEW_OBSERVATION},
                      quota=_inline_schema("Quota", "harness"), rework_limit={"type": "integer", "minimum": 0, "maximum": 32},
                      prior_report_ref={"anyOf": [_ARTIFACT, {"type": "null"}]}, idempotency_key=_KEY)),
    "nf_harness_review_withdraw": ("Withdraw a review written by this assigned reviewer.",
        object_schema(report_ref=_ARTIFACT, reason=_REASON, idempotency_key=_KEY)),
    "nf_harness_review_decide": ("Record an authorized editorial response to the cited draft and review.",
        object_schema(optional=("native_decision",), task_id=_UUID, generation={"type": "integer", "minimum": 1}, report_ref=_ARTIFACT,
                      action={"enum": ["RETURN", "RETAIN", "ACCEPT_REVISION", "DEFER", "WITHDRAW"]},
                      reason=_REASON, native_decision=_inline_schema("EditDecisionRequest", "editorial"), idempotency_key=_KEY)),
    "nf_harness_revision_brief": ("Read the returned author-facing findings for the assigned manuscript.",
        object_schema(report_ref=_ARTIFACT)),
    "nf_harness_revise": ("Revise only the assigned finding spans and retain the source draft.",
        object_schema(report_ref=_ARTIFACT, scene_ref=_ARTIFACT,
                      replacements={"type": "array", "minItems": 1, "maxItems": 64,
                                    "items": object_schema(finding_id=_UUID, replacement={"type": "string", "maxLength": 32000})},
                      idempotency_key=_KEY)),
    "nf_harness_plan_propose": ("Derive bounded conditional alternatives from the assigned plan.",
        object_schema(optional=("horizon_scenes",), plan_ref=_ARTIFACT,
                      node_ids={"type": "array", "items": _UUID, "minItems": 1, "maxItems": 8, "uniqueItems": True},
                      trigger={"enum": ["ORDINARY", "MOTIVE_CONFLICT", "IRREVERSIBLE"]},
                      quota=_inline_schema("Quota", "harness"), horizon_scenes={"type": "integer", "minimum": 1, "maximum": 32},
                      idempotency_key=_KEY)),
    "nf_harness_simulate": ("Evaluate a frozen candidate using enrolled actors' fixed knowledge cuts.",
        object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1}, candidate_ref=_ARTIFACT,
                      actor_tasks={"type": "array", "minItems": 1, "maxItems": 16, "uniqueItems": True,
                                   "items": object_schema(task_id=_UUID, generation={"type": "integer", "minimum": 1})},
                      max_branches={"type": "integer", "minimum": 1, "maximum": 32},
                      max_steps_per_branch={"type": "integer", "minimum": 1, "maximum": 128},
                      seed={"type": "integer", "minimum": 0, "maximum": 2147483647}, idempotency_key=_KEY)),
    "nf_harness_plan_decide": ("Submit a plan decision to the existing adoption service, or decline every alternative.",
        object_schema(optional=("rolling_review_ref",), task_id=_UUID, generation={"type": "integer", "minimum": 1}, candidate_ref=_ARTIFACT,
                      option_id={"anyOf": [_UUID, {"type": "null"}]},
                      rolling_review_ref={"anyOf": [_ARTIFACT, {"type": "null"}]}, idempotency_key=_KEY)),
    "nf_change_point_request": ("Record evidence for a bounded revision to the volume plan.",
        object_schema(volume=_UUID, prior_plan_ref=_ARTIFACT,
                      event_refs={"type":"array","minItems":1,"maxItems":32,"items":_EVENT},
                      reason=_REASON, idempotency_key=_KEY)),
    "nf_rolling_window_submit": ("Submit a bounded future window for independent review.",
        object_schema(change_point_ref=_ARTIFACT, window={"type":"object"}, idempotency_key=_KEY)),
    "nf_change_point_review": ("Record an independent decision on the exact candidate and window.",
        object_schema(change_point_ref=_ARTIFACT, plan_ref=_ARTIFACT, window_ref=_ARTIFACT,
                      decision={"enum":["APPROVED","RETURNED"]}, reason=_REASON, idempotency_key=_KEY)),
    "nf_task_submit": ("Durably submit an authorized task and return its receipt.",
                       _inline_schema("TaskCreateRequest", "http")),
    "nf_task_status": ("Read the authorized task state.", object_schema(task_id=_UUID)),
    "nf_task_cancel": ("Cancel the authorized task at its expected generation.",
                       _inline_schema("TaskCancelHttpRequest", "http")),
    "nf_task_events": ("Read a page of state transitions for the authorized task.",
                       object_schema(optional=("cursor", "limit"), task_id=_UUID, cursor=_UUID,
                                     limit={"type": "integer", "minimum": 1, "maximum": 100})),
    "nf_project_status": ("Read task counts for the configured project branch.", object_schema()),
    "nf_resume_project": ("Recover eligible work and wake satisfied waits in the configured branch.", object_schema()),
    "nf_task_get": ("Read the bound task's current execution identity.", object_schema()),
    "nf_context_get": ("Read the bound task's authorized context.", object_schema()),
    "nf_query_batch": ("Read selected state values at this task's bound time and knowledge cut.",
                       object_schema(lookups={"type": "array", "minItems": 1, "maxItems": 32, "items": _LOOKUP})),
    "nf_artifact_put": ("Store a task candidate with a stable idempotency key.",
                        object_schema(content={"type": "string", "minLength": 1, "maxLength": 32_768},
                                      idempotency_key={"type": "string", "minLength": 1, "maxLength": 96})),
}
_TOOLS["nf_change_point_request"][1]["properties"]["assessment"] = _inline_schema("DeviationAssessment", "agent")
_VALIDATORS = {name: Draft202012Validator(schema, format_checker=FormatChecker())
               for name, (_, schema) in _TOOLS.items()}
