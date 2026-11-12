"""Read released instructions from the exact selection frozen into an NF task input."""

from novel_factory.application.kernel import require
from novel_factory.domain.canonical import fingerprint

_LABEL = {
    "ROLE_INSTRUCTIONS": "Approved role method",
    "SKILL_INSTRUCTIONS": "Approved skill instructions",
    "REVIEW_RUBRIC": "Approved independent review guidance",
    "CONTEXT_RECIPE": "Reading order and use of the supplied materials",
}


def render_instruction_blocks(blocks):
    sections = []
    for block in blocks:
        sections.append(_LABEL[block["change_kind"]] + ":\n" + block["body"])
        if block["change_kind"] == "REVIEW_RUBRIC" and block["review_checks"]:
            sections.append("Review checks: " + ", ".join(block["review_checks"]))
    return "\n" + "\n".join(sections) if sections else ""


def organize_materials(materials, blocks):
    """Keep all and only the already-authorized section values in the released order."""
    orders = [
        block["material_order"] for block in blocks if block["change_kind"] == "CONTEXT_RECIPE"
    ]
    if not orders:
        return materials
    present = [name for name in orders[-1] if name in materials]
    require(
        len(set(present)) == len(present),
        "INVALID_ARGUMENT",
        "Duplicate creative material priority",
    )
    names = present + sorted(materials.keys() - set(present))
    return [{"section": name, "items": materials[name]} for name in names]


def read_selection(kernel, repo, world_ref, selection_ref):
    """Reconstruct the immutable lineage. Pointer changes never rewrite historical assignments."""
    from .adaptive_instructions import load_instruction

    seen, blocks = set(), []
    ref = selection_ref
    target = None
    body_bytes = 0
    while ref is not None:
        require(
            ref["artifact_revision_id"] not in seen and len(seen) < 32,
            "INSTRUCTION_LINEAGE_INVALID",
            "Instruction lineage repeats or exceeds its bound",
        )
        seen.add(ref["artifact_revision_id"])
        row = kernel.artifact(
            repo, ref, world_ref["work_id"], kind="CREATIVE_INSTRUCTION_SELECTION"
        )
        data = row["payload"]
        scope = data["scope"]["world_ref"]
        require(
            fingerprint(data) == ref["sha256"]
            and scope["work_id"] == world_ref["work_id"]
            and scope["branch_id"] == world_ref["branch_id"],
            "VIEW_DENIED",
            "Instruction selection is outside this work or branch",
        )
        identity = tuple(
            data[key]
            for key in (
                "role_id",
                "task_kind",
                "skill_name",
                "dynamic_instance_ref",
                "baseline_runtime_sha256",
                "scope_key",
            )
        )
        if target is None:
            target = identity
        require(
            target == identity,
            "TASK_REBIND_REQUIRED",
            "Instruction lineage changed runtime or employee scope",
        )
        block = load_instruction(kernel, repo, scope, data["instruction_ref"])
        require(
            (block["role_id"], block["task_kind"], block["skill_name"]) == identity[:3]
            and block["change_kind"] == data["change_kind"]
            and block.get("source_adaptive_ref") == data.get("source_adaptive_ref"),
            "INVALID_EVIDENCE",
            "Instruction bytes do not match the released scope",
        )
        body_bytes += len(block["body"].encode())
        require(
            body_bytes <= 200000,
            "CAPACITY_LIMIT",
            "Released instruction bytes exceed the runtime limit",
        )
        blocks.append(block)
        ref = data.get("source_adaptive_ref")
    blocks.reverse()
    return blocks


def resolve_for_input(kernel, repo, spec, data, runtime):
    ref = data["adaptive_instruction_ref"]
    world = spec["world_ref"]
    row = kernel.artifact(repo, ref, world["work_id"], kind="CREATIVE_INSTRUCTION_SELECTION")[
        "payload"
    ]
    instance = None
    if data.get("dynamic_assignment_ref"):
        frozen = kernel.artifact(
            repo, data["dynamic_assignment_ref"], world["work_id"], kind="CREATIVE_DYNAMIC_FREEZE"
        )["payload"]
        instance = frozen["instance_ref"]
    require(
        (row["role_id"], row["task_kind"], row["skill_name"], row["dynamic_instance_ref"])
        == (spec["role_id"], spec["task_kind"], runtime["skill_name"], instance)
        and row["baseline_runtime_sha256"]
        == spec["runtime_contract_version"]
        == data["runtime_sha256"],
        "TASK_REBIND_REQUIRED",
        "Published instructions belong to a different runtime or employee",
    )
    return read_selection(kernel, repo, world, ref)


def review_with_guidance(
    layer, text_ref, text, observations, *, related_texts, verified_evidence, blocks
):
    """Supplement a review with approved native checks, retaining the reader's explicit interpretation."""
    from novel_factory.domain.creative_review import evaluate

    inputs = {"related_texts": related_texts, "verified_evidence": verified_evidence}
    findings = evaluate(layer, text_ref, text, observations, **inputs)
    checks = {
        name
        for block in blocks
        if block["change_kind"] == "REVIEW_RUBRIC"
        for name in block["review_checks"]
    }
    if not checks or not observations:
        return findings
    located = {(obs["pattern"], obs["start"], obs["end"]) for obs in observations}
    for item in evaluate(layer, text_ref, text, [], **inputs):
        span = item["finding"]["text_span"]
        location = (item["pattern"], span["start"], span["end"])
        if item["pattern"] in checks and location not in located and len(findings) < 64:
            findings.append(item)
            located.add(location)
    return findings
