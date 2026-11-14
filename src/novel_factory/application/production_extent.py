"""Verify a final P6 submission against its original length and native writer receipts."""
from hashlib import sha256
from unicodedata import name

from ..domain.canonical import fingerprint
from ..infrastructure.creative_config import CreativeHarnessConfig
from .agent_gateway import AgentGateway
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .planning import PlanningService
from .production_pipeline import _artifact
from .statistics import is_fixture_configuration
from .volume_successor import _selected


def validate_length_bounds(length):
    """A chapter target may specify only a minimum. Han allocation needs both bounds."""
    fields = set(length)
    required = {"preset", "unit", "min"}
    lower = length.get("min")
    upper = length.get("max")
    unit = length.get("unit")
    require(unit in ("chapters", "han_characters") and type(lower) is int and lower >= 1
            and ((fields == required and unit == "chapters")
                 or (fields == required | {"max"} and type(upper) is int and upper >= lower)),
            "INVALID_ARGUMENT", "Invalid explicit manuscript range")


def within_length(length, quantity):
    return (type(quantity) is int and quantity >= length["min"]
            and ("max" not in length or quantity <= length["max"]))


def bind_extent(kernel, repo, owner, wr, length):
    """Store the user's exact inclusive range, or the explicit absence of a hard range."""
    work, branch = wr["work_id"], wr["branch_id"]
    repo.one("works", {"work_id": work}, lock="update")
    kernel.authorize(repo, owner, work, "ADMIN", branch)
    require(kernel.head(repo, work, branch) == wr, "HEAD_CONFLICT", "World changed before length binding")
    if isinstance(length, dict):
        validate_length_bounds(length)
    else:
        require(isinstance(length, str) and length.strip(), "INVALID_ARGUMENT", "Missing length selection")
    key = "P6_EXTENT:" + branch
    body = {"length": length}
    prior = _selected(repo, work, key)
    if prior:
        require(kernel.artifact(repo, prior, work, kind="MANUSCRIPT_EXTENT")["payload"] == body,
                "TASK_REBIND_REQUIRED", "The original length is already bound")
        return prior
    ref = _artifact(kernel, repo, work, owner, "MANUSCRIPT_EXTENT", body)
    PlanningService(kernel)._select(repo, wr, key, ref)
    return ref


def _count_han(text):
    # Count unified/compatibility ideographs assigned by Python's Unicode database,
    # plus the ideographic number zero. Titles and generated metadata are separate.
    return sum(ch == "〇" or name(ch, "").startswith(("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-"))
               for ch in text)


def _chapter_count(kernel, repo, wr, cursor, draft_refs):
    if not cursor:
        return None
    work = wr["work_id"]
    previous = cursor["last_checkpoint"]
    # Use the frozen writer coverage in each chapter. Scene count alone cannot
    # establish chapter count: e.g. 100 scenes may all belong to one chapter.
    count, chapters, ordered = 0, set(), []
    for expected in reversed(range(cursor["window_count"])):
        require(previous, "RELEASE_INCOMPLETE", "Missing chapter checkpoint")
        checkpoint = kernel.artifact(repo, previous, work, kind="ROLLING_CHECKPOINT")["payload"]
        require(checkpoint["index"] == expected and checkpoint["run_id"] == cursor["run_id"]
                and checkpoint["world_ref"]["branch_id"] == wr["branch_id"],
                "RELEASE_INCOMPLETE", "Checkpoint series changed")
        for chapter in reversed(checkpoint["chapters"]):
            require(chapter["chapter_id"] not in chapters and chapter["ordinal"] == cursor["chapter_count"] - count,
                    "RELEASE_INCOMPLETE", "Missing or duplicate chapter identity")
            chapters.add(chapter["chapter_id"])
            count += 1
            ordered.extend(s["draft_ref"] for s in reversed(chapter["scenes"]))
        previous = checkpoint["previous"]
    require(previous is None and count == cursor["frozen_chapters"] == cursor["chapter_count"]
            and draft_refs == list(reversed(ordered)), "RELEASE_INCOMPLETE",
            "Final text does not cover all frozen chapters in order")
    return count


def check_length(length, characters, chapters):
    """Historical length requests are advisory; completion depends on story review.

    Keep this compatibility entry point while preserving the measurements and
    native writer provenance collected by ``measure_submission``.
    """
    return


def _real_candidate(delivery, repo, task, *, design_revision_read=False, ending_plan_read=False):
    audit, candidate = delivery.audited_candidate(repo, task, production_accounting=True,
                                                  design_revision_read=design_revision_read, ending_plan_read=ending_plan_read)
    require(audit["execution_kind"] in {"REAL_EXTERNAL", "REAL_HOST"} and audit["provider_receipt"]
            and not is_fixture_configuration(audit["model_configuration"])
            and audit["model_configuration"].get("model") and audit["role_id"] == task["role_id"]
            and audit["task_id"] == task["task_id"] and audit["generation"] == task["generation"],
            "INVALID_EVIDENCE", "Production output lacks a real native provider receipt")
    call = repo.one("call_attempts", {"attempt_id": audit["attempt_id"], "task_id": task["task_id"],
                                      "generation": task["generation"]})
    require(call and call["provider_receipt"] == audit["provider_receipt"],
            "CALL_RECONCILIATION_REQUIRED", "The production attempt has a different supplier receipt")
    return audit, candidate


def _idea_evidence(kernel, repo, wr, extent_ref, length, delivery):
    row = repo.one("receipts", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                   "command_type": "P6_IDEA_PROVENANCE", "idempotency_key": "initial"})
    if not row:
        return "OPERATOR_PLANNED"
    receipt = row["payload"]
    source = receipt["source_world_ref"]
    require(receipt["length_ref"] == extent_ref and receipt["creative_input"]["length"] == length
            and source["work_id"] != wr["work_id"], "INVALID_EVIDENCE", "Original idea length binding changed")
    require(all(receipt["world_ref"][k] == wr[k] for k in ("work_id", "branch_id")),
            "INVALID_EVIDENCE", "The adopted design belongs to another work or branch")
    design_ref = kernel.artifact(repo, receipt["design_ref"], wr["work_id"], kind="INITIAL_STORY_DESIGN")
    from .design_release_evidence import verify_adopted_design
    verify_adopted_design(kernel, repo, delivery, receipt, design_ref["payload"])
    return "NATIVE_IDEA"


def _writer_evidence(delivery, kernel, repo, wr, draft, text_ref):
    work, branch = wr["work_id"], wr["branch_id"]
    origin = draft.get("agent_output_ref")
    require(origin and text_ref == draft["text_ref"] and draft["input_world_ref"]["branch_id"] == branch,
            "INVALID_EVIDENCE", "Every P6 draft requires its own native model output")
    task = repo.one("tasks", {"work_id": work, "branch_id": branch, "task_id": draft["task_id"],
                              "generation": draft["generation"]})
    require(task and task["state"] == "ACCEPTED" and task["role_id"] == "writer"
            and task["context_bundle_id"] == draft["context_bundle_id"], "INVALID_EVIDENCE",
            "Manuscript writer is not the accepted generation and context")
    audit, candidate = _real_candidate(delivery, repo, task)
    require(audit["output_ref"] == origin and audit["world_ref"] == draft["input_world_ref"],
            "INVALID_EVIDENCE", "Draft version differs from the native writer output")
    text = candidate.get("text")
    require(isinstance(text, str) and sha256(text.encode("utf-8")).hexdigest() == text_ref["sha256"],
            "INVALID_EVIDENCE", "Submitted prose differs from its audited model candidate")
    kernel.objects.verify(text_ref["sha256"])
    return text, audit["attempt_id"]


def measure_submission(kernel, repo, request, entries, text_refs):
    """Run within the release service's authorized read/commit, before scoring and freezing."""
    from .rolling_production import _load as load_rolling

    wr = request["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    extent_ref = _selected(repo, work, "P6_EXTENT:" + branch)
    length = kernel.artifact(repo, extent_ref, work, kind="MANUSCRIPT_EXTENT")["payload"]["length"] if extent_ref else None
    cursor = load_rolling(kernel, repo, work, branch)
    draft_refs = []
    if cursor:
        require(cursor["final_volume_id"] == request["volume_id"], "RELEASE_INCOMPLETE", "Expected the final volume")
        for release_ref in cursor["release_refs"]:
            record = kernel.artifact(repo, release_ref, work, kind="RELEASE_RECORD")["payload"]
            if record["volume_id"] != request["volume_id"]:
                draft_refs.extend(record["ordered_draft_refs"])
    draft_refs.extend(entry["acceptance"]["draft_ref"] for entry in entries)
    require(draft_refs and len(draft_refs) == len(text_refs), "INVALID_EVIDENCE", "Draft and text counts differ")
    chapter_count = _chapter_count(kernel, repo, wr, cursor, draft_refs)
    # The candidate delivery reader only consults durable audit artifacts.
    delivery = CandidateDelivery(kernel, AgentGateway(kernel, {}, creative_harness=CreativeHarnessConfig()))
    source = _idea_evidence(kernel, repo, wr, extent_ref, length, delivery)
    characters = 0
    attempts = []
    for draft_ref, text_ref in zip(draft_refs, text_refs, strict=True):
        draft = kernel.artifact(repo, draft_ref, work, kind="NARRATIVE_DRAFT")["payload"]
        text, attempt = _writer_evidence(delivery, kernel, repo, wr, draft, text_ref)
        characters += _count_han(text)
        attempts.append(attempt)
    require(len(set(attempts)) == len(attempts), "INVALID_EVIDENCE", "Each draft needs a distinct writer attempt")
    check_length(length, characters, chapter_count)
    return {"source": source, "length_ref": extent_ref, "length": length, "han_characters": characters, "chapters": chapter_count,
            "scenes": len(draft_refs), "writer_attempts": len(attempts), "writer_attempts_sha256": fingerprint(attempts)}
