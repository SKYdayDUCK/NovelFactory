"""Compile a literary reading view from the current, already authorized NF delivery.

The gateway owns all reads and fences. This function has no database, object
store or model access. Its first result is controller evidence; its second
result is the complete, deterministically projected user message.
"""
from __future__ import annotations

import json
from functools import lru_cache
from hashlib import sha1, sha256

from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog


def _blob_oid(text):
    data = text.encode("utf-8")
    return sha1(b"blob " + str(len(data)).encode("ascii") + b"\x00" + data,
                usedforsecurity=False).hexdigest()


@lru_cache(maxsize=1)
def _builtin_provenance():
    # The release manifest comes from git ls-tree at the pinned source revision.
    # RuntimeCatalog locates the same packaged resources in a wheel or checkout.
    resource = RuntimeCatalog().root / "provenance.json"
    try:
        return json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError("RUNTIME_UNAVAILABLE", "Built-in creative runtime provenance is unavailable") from None


def runtime_provenance(runtime, frozen=None):
    expected = {"role_blob": _blob_oid(runtime["role_contract"]),
                "skill_blob": _blob_oid(runtime["skill_instructions"])}
    if frozen is None:
        current = _builtin_provenance()
        # Pre-upgrade tasks contain their selected text. Match both blobs from
        # a single release, including for a task dispatched after deployment.
        for release in (current, *current.get("previous", ())):
            if (release["roles"].get(runtime["role_id"]) == expected["role_blob"]
                    and release["skills"].get(runtime["skill_name"]) == expected["skill_blob"]):
                return {"source_commit": release["source_commit"], **expected}
        raise DomainError("RUNTIME_UNAVAILABLE", "No released source matches these frozen instructions")
    require(all(frozen.get(key) == value for key, value in expected.items()),
             "RUNTIME_UNAVAILABLE", "No verified source revision for these frozen instructions")
    return {"source_commit": frozen["source_commit"], **expected}


def _decode(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError, RecursionError):
        return value


def _fact(statement):
    """Preserve distinctions between an absent value, unknown, and false."""
    value = _decode(statement)
    if not isinstance(value, dict) or not {"subject_id", "predicate", "value_status", "value"} <= value.keys():
        return statement
    target = value["value"]
    if isinstance(target, dict):
        if target.keys() == {"kind", "value"}:
            target = target["value"]
        elif target.keys() == {"kind", "entity_id"}:
            target = target["entity_id"]
    return {"subject": value["subject_id"], "relation": value["predicate"].replace("_", " "),
            "meaning": value["value_status"].lower().replace("_", " "), "value": target}


def _style(guide):
    return {"principles": guide["principles"],
            "voices": [{key: voice[key] for key in ("name", "actor_id", "register", "allowed_variations")}
                       for voice in guide["voices"]]}


def _boundaries(checks):
    result = []
    for check in checks:
        item = {"subject": check["subject_id"], "relation": check["predicate"].replace("_", " "),
                "meaning": check["expected_status"].lower().replace("_", " "), "value": check["expected_value"]}
        if check["plane"] == "BELIEF":
            item["believed_by"] = check["actor_id"]
            item["known_at"] = check["known_at"]
        item["at"] = check["at"]
        result.append(item)
    return result


def _project_material(item):
    kind = item["kind"]
    if not item.get("scoped_delivery") and kind == "TEXT":
        return "prose", item["content"]
    value = _decode(item["content"])
    if not item.get("scoped_delivery"):
        return ("structured_evidence", value) if isinstance(value, (dict, list)) else ("evidence", item["content"])
    if kind == "STYLE_GUIDE" and isinstance(value, dict):
        guide = value.get("style", value)
        if "principles" in guide and "voices" in guide:
            return "style", _style(guide)
    if kind == "SCENE_PLAN" and isinstance(value, dict) and "scene_contract" in value:
        card = value["scene_contract"]
        return "scene", {"purpose": value["literary_purpose"], "narrator": card["narrator_mode"],
                         "point_of_view": card["pov_actor_id"], "allowed_invention": card["allowed_invention"],
                         "story_interval": card["story_interval"], "reader_ordinal": card["reader_cut_ordinal"],
                         "opening": _boundaries(value["entry_checks"]), "closing": _boundaries(value["exit_checks"]),
                         "disclosures": [{k: v[k] for k in ("statement", "truth_status")}
                                         for v in value["allowed_disclosures"]],
                         "styles": [_style(s["style"]) for s in value["selected_styles"]],
                         **({"predecessor_passages": [p["text"] for p in value["predecessor_passages"]]}
                            if value.get("predecessor_passages") else {})}
    if kind in {"NARRATIVE_DRAFT", "TEXT"} and isinstance(value, dict) and "text" in value and "span" in value:
        text = value["text"]
        span = value["span"]
        require(span["unit"] == "UNICODE_CODEPOINT" and 0 <= span["start"] < span["end"]
                and span["end"] - span["start"] == len(text)
                and span["quote_sha256"] == sha256(text.encode("utf-8")).hexdigest(),
                "INVALID_EVIDENCE", "Delivered prose does not match its fixed span")
        return "prose", text
    if kind == "LITERARY_MEMORY" and isinstance(value, dict) and "interpretation" in value:
        return "reading", {k: value[k] for k in ("entry_kind", "interpretation", "subject_id", "unresolved_items")}
    if kind == "CONTINUITY_SNAPSHOT" and isinstance(value, dict) and "entries" in value:
        return "continuity", [{k: e[k] for k in ("kind", "status", "interpretation", "epistemic_status")}
                              for e in value["entries"]]
    # This is the result of the scoped kernel evidence read. Preserve exact
    # quoted testimony, including braces in prose; never promote its text into
    # the system prompt or reinterpret it as an instruction.
    if isinstance(value, (dict, list)):
        return "structured_evidence", value
    return "evidence", item["content"]


def build_context_packet(binding, materials, editorial=None, joint=None, proposal_protocol=None):
    task, grant, spec = (binding[key] for key in ("task", "grant", "spec"))
    context, compiled = binding["context"], binding["compiled_context"]
    bundle, read_cut = context["payload"], context["read_cut"]
    scope = {"world_ref": spec["world_ref"], "task_id": task["task_id"], "generation": task["generation"]}
    cut = read_cut["knowledge_cut"]
    require(compiled["bundle"] == bundle and compiled["read_cut"] == read_cut
             and all(bundle[k] == v for k, v in scope.items())
             and bundle["context_bundle_id"] == task["context_bundle_id"]
             and bundle["role_id"] == grant["role_id"] == spec["role_id"]
             and bundle["actor_id"] == grant["actor_id"] == cut.get("actor_id")
             and read_cut["permission_epoch"] == grant["epoch"],
             "VIEW_DENIED", "Creative input has no matching fixed context and grant")
    restricted = cut["plane"] == "BELIEF"
    require(grant["role_id"] != "character_actor" or restricted,
             "VIEW_DENIED", "Character creative input requires BELIEF")
    require(not restricted or (not editorial and not joint),
             "VIEW_DENIED", "Editorial material cannot enter a character's knowledge")
    runtime = binding["runtime"]
    digest = fingerprint(runtime)
    require(digest == spec["runtime_contract_version"] == binding["input"]["runtime_sha256"],
             "TASK_REBIND_REQUIRED", "The frozen literary role or skill changed")
    runtime_ref = {"role_id": runtime["role_id"], "task_kind": runtime["task_kind"],
                   "role_version": runtime["role_contract_version"], "skill_name": runtime["skill_name"],
                   "skill_version": runtime["skill_version"], "runtime_sha256": digest,
                   **runtime_provenance(runtime, binding.get("runtime_provenance"))}
    authority = {"principal_id": context["principal_id"], "grant_id": context["grant_id"],
                 "permission_epoch": grant["epoch"]}
    permitted = {fingerprint(e): e for e in bundle["permitted_evidence_refs"]}
    sources = {}
    creative = {}
    structured = []
    omissions = list(compiled["omitted_optional_scopes"])

    def select(evidence, visibility, reason, content):
        key = fingerprint(evidence)
        if key not in sources:
            require(len(sources) < 64, "CONTEXT_INCOMPLETE", "Too many required creative evidence sources")
            sources[key] = {"evidence": evidence, "authority": authority, "world_ref": scope["world_ref"],
                            "visibility": visibility, "selection_reason": reason, "parts": []}
        sources[key]["parts"].append(content)

    for f in bundle["facts"]:
        require(not restricted or f["status"] == "BELIEF", "VIEW_DENIED", "Incompatible character perspective")
        name = {"BELIEF": "beliefs", "FACT": "facts", "HYPOTHESIS": "hypotheses"}[f["status"]]
        statement = _fact(f["statement"])
        creative.setdefault(name, []).append(statement)
        if (task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract"):
            structured.append({"value": {"statement": f["statement"], "status": f["status"],
                                         "evidence_refs": f["evidence_refs"]}})
        for e in f["evidence_refs"]:
            require(fingerprint(e) in permitted, "VIEW_DENIED", "Fact evidence is outside the compiled cut")
            select(e, cut["plane"], "Source of a selected fixed-cut statement.", statement)
    if (cut["plane"] == "WORLD" and (task["role_id"], task["task_kind"])
            == ("evidence_maintainer", "assertion_extract")):
        for section in compiled["sections"]:
            if section["section_kind"] != "EVIDENCE":
                continue
            value = _decode(section["content"])
            if not isinstance(value, dict) or value.get("kind") != "FACT_READING_SCOPE":
                continue
            require(value["world_ref"] == bundle["world_ref"] and value["story_time"] == bundle["story_time"],
                    "VIEW_DENIED", "Fact reading metadata differs from the compiled cut")
            # _binding verified this artifact's hash, role, manifest, grant and
            # fixed WorldRef. Keep unknown typed answers intact, rather than
            # inventing facts or names from the manuscript.
            compiled_ref = binding["compiled_ref"]
            structured.append({"ref": compiled_ref, "value": value})
            select({"kind": "ARTIFACT", "artifact_ref": compiled_ref}, "WORLD",
                   "Authorized identity labels and typed state answers from this fixed compiler output.", value)
    if cut["plane"] == "PLAN":
        for section in compiled["sections"]:
            if section["section_kind"] == "PLAN":
                for entry in json.loads(section["content"]):
                    summary = entry["safe_summary"]
                    creative.setdefault("conditional_plans", []).append(summary)
                    for e in entry["evidence_refs"]:
                        require(fingerprint(e) in permitted, "VIEW_DENIED", "Plan evidence is outside the compiled cut")
                        select(e, "PLAN", "Source of a conditional planning summary.", summary)
    # Core exposes two fixed compiler diagnostics. The record keeps their
    # exact wording. Only their narrative implications belong in the reading
    # view. Future compiler diagnostics need an explicit projection.
    cautions = {
        "Requested material is not available as a determined value at this knowledge cut.":
            "Some details are unknown from this point of view.",
        "Plans are conditional editorial material; they do not apply world effects.":
            "These possible developments have not happened.",
    }
    for limitation in bundle["limitations"]:
        if limitation in cautions:
            creative.setdefault("uncertainties", []).append(cautions[limitation])
        else:
            omissions.append("An unmapped compiler limitation was retained in the control record.")

    # Input order is part of the frozen task; deduplicate repeated identical
    # references without changing the original order.
    seen_materials = set()
    for item in materials:
        ref = item["artifact_ref"]
        marker = fingerprint(ref)
        if marker in seen_materials:
            continue
        seen_materials.add(marker)
        evidence = {"kind": "ARTIFACT", "artifact_ref": ref}
        kernel_evidence = next((e for e in permitted.values()
                                if e.get("artifact_ref") == ref and "span" not in e), None)
        require(kernel_evidence is not None or item.get("scoped_delivery") is True,
                 "VIEW_DENIED", "Source lacks a task-scoped material delivery")
        if kernel_evidence is not None:
            evidence = kernel_evidence
        if restricted:
            require(kernel_evidence is not None and item["kind"] not in {"STYLE_GUIDE", "SCENE_PLAN", "NARRATIVE_DRAFT",
                                              "LITERARY_MEMORY", "CONTINUITY_SNAPSHOT"},
                     "VIEW_DENIED", "Source is outside this character's knowledge")
        name, content = _project_material(item)
        if name == "structured_evidence":
            structured.append({"ref": ref, "value": content})
        else:
            creative.setdefault(name, []).append(content)
        select(evidence, cut["plane"] if name in {"structured_evidence", "evidence"} or restricted else "LITERARY",
               "Assigned material delivered through the task-scoped reader.", content)
        if name == "prose" and item.get("scoped_delivery"):
            delivery = _decode(item["content"])
            if isinstance(delivery, dict) and {"text_ref", "span"} <= delivery.keys():
                select({"kind": "ARTIFACT", "artifact_ref": delivery["text_ref"], "span": delivery["span"]},
                       cut["plane"] if restricted else "LITERARY", "Exact text range offered by the assigned reader.", content)
                if ((task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract")
                        and "required_event_refs" in delivery and "declared_important_deltas" in delivery):
                    obligations = {key: delivery[key] for key in (
                        "draft_ref", "required_event_refs", "declared_important_deltas"
                    )}
                    if "passage_index" in delivery:
                        obligations["passage_index"] = delivery["passage_index"]
                    structured.append({"ref": ref, "value": obligations})
                    select(evidence, cut["plane"], "Reconciliation obligations of the assigned draft.", obligations)
                if task["role_id"] == "reviewer":
                    # The literary view keeps prose unlabelled; a chapter can contain several
                    # distinct drafts. Bind issue.subject_ref to the corresponding passage.
                    pointer = {"prose_index": len(creative["prose"]) - 1, "draft_ref": delivery["draft_ref"],
                               "text_ref": delivery["text_ref"], "span": delivery["span"]}
                    structured.append({"ref": ref, "value": pointer})
                    select(evidence, cut["plane"], "Exact passage for a review finding.", pointer)
        if name == "scene":
            card = _decode(item["content"])
            events = {"required_event_refs": card["required_event_refs"],
                      "event_refs": card["scene_contract"]["event_refs"]}
            if any(events.values()):
                structured.append({"ref": ref, "value": events})
                select(evidence, cut["plane"], "Events bound to this writing card.", events)
            for style in card["selected_styles"]:
                select({"kind": "ARTIFACT", "artifact_ref": style["source_ref"]}, "LITERARY",
                       "Style bound to the selected scene.", _style(style["style"]))

    unused = len(permitted.keys() - sources.keys())
    if unused:
        omissions.append(f"{unused} permitted evidence references had no selected statement or assigned material.")
    if not restricted:
        scene_style = any(scene["styles"] for scene in creative.get("scene", ()))
        if not creative.get("style") and not scene_style:
            omissions.append("No assigned style or voice guide.")
        if not any(creative.get(name) for name in ("prose", "scene", "continuity")):
            omissions.append("No assigned adjacent prose, scene boundary or continuity notes.")
    else:
        omissions.append("Only this character's fixed knowledge cut was selected.")

    if binding.get("adaptive_blocks"):
        from .adaptive_runtime import organize_materials
        creative = organize_materials(creative, binding["adaptive_blocks"])
    user = {"assignment": binding["input"]["assignment"], "creative_materials": creative}
    # Typed outputs can require exact citations and operation handles. Keep
    # them outside the literary view. Neither section has system authority.
    if proposal_protocol:
        user["proposal_protocol"] = proposal_protocol
    if structured:
        user["structured_evidence"] = structured
    if editorial or joint:
        user["candidate_workflow"] = {key: value for key, value in (("editorial", editorial), ("joint", joint)) if value}
    if editorial:
        for entry in editorial.get("materials", {}).values():
            select({"kind": "ARTIFACT", "artifact_ref": entry["ref"]}, cut["plane"],
                   "Authorized editorial operation input.", entry["content"])
    if joint:
        select({"kind": "RULE", "artifact_ref": joint["ruleset_ref"]}, cut["plane"],
               "Ruleset for the enrolled joint interaction.", joint["ruleset"])
        for entry in joint.get("action_proposals", []):
            select({"kind": "ARTIFACT", "artifact_ref": entry["ref"]}, cut["plane"],
                   "Sealed proposal released to the assigned adjudicator.", entry)
    if sources:
        user["response_references"] = [s["evidence"] for s in sources.values()]
    for source in sources.values():
        source["content_sha256"] = fingerprint(source.pop("parts"))
    budget = min(spec["budget"]["token_budget"], 2147483647)
    require(len(canonical_bytes(user)) <= budget, "CONTEXT_INCOMPLETE",
             "Creative materials exceed the task context budget")
    require(len(omissions) <= 64, "CONTEXT_INCOMPLETE", "Too many omitted creative scopes")
    record = {"schema_version": "1.0", "kind": "CONTEXT_PACKET", "scope": scope,
              "context": bundle, "knowledge_cut": cut, "runtime": runtime_ref,
              "sources": list(sources.values()), "omissions": omissions,
              "token_budget": budget, "output_status": "NON_AUTHORITATIVE"}
    validate_contract("ContextPacket", record, "harness")
    return record, user
