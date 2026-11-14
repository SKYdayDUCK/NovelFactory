"""One lossless writer wire repair, never a general JSON or literary repair.

Only the missing closing quote of a frozen top-level WorldRef UUID is eligible.
Original response bytes and supplier accounting remain authoritative evidence.
"""
import hashlib
import json
import re
from uuid import UUID, uuid5

from ..contracts.registry import validate_contract
from ..domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from ..domain.errors import DomainError
from ..infrastructure.wire_receipts import read_receipt
from .kernel import require

KIND = "NORMALIZED_WRITER_FORMAT"
POLICY = "nf-writer-worldref-closing-quote-v1"
FIELDS = ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")
UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
WORLD = re.compile(r"\{\s*" + r"\s*,\s*".join(
    r'"(?P<key' + str(i) + r'>work_id|branch_id|world_revision_id)"\s*:\s*"(?P<value' + str(i) + r'>'
    + UUID_PATTERN + r')(?P<close' + str(i) + r'>")?\s*' for i in range(3)) + r"\}")


def _check(value):
    require(value, "INVALID_MODEL_OUTPUT", "Writer output has no unique lossless frozen-identity normalization")


def _provider_json(text):
    """HTTP metadata can contain floats; duplicate keys/non-JSON constants cannot."""
    def pairs(rows):
        result = {}
        for key, value in rows:
            _check(key not in result)
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs, parse_constant=lambda _value: _check(False))


def _value_start(text, wanted):
    """Walk complete top-level members; never search inside literary strings."""
    decoder = json.JSONDecoder()
    cursor = len(text) - len(text.lstrip())
    _check(cursor < len(text) and text[cursor] == "{")
    cursor += 1
    seen = set()
    while cursor < len(text):
        cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
        key, end = decoder.raw_decode(text, cursor)
        _check(type(key) is str and key not in seen)
        seen.add(key)
        cursor = end + len(text[end:]) - len(text[end:].lstrip())
        _check(text[cursor:cursor + 1] == ":")
        cursor += 1
        cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
        if key == wanted:
            return cursor
        _, cursor = decoder.raw_decode(text, cursor)
        cursor += len(text[cursor:]) - len(text[cursor:].lstrip())
        _check(text[cursor:cursor + 1] == ",")
        cursor += 1
    _check(False)


def derive(text, identity, output_contract):
    """Pure syntax derivation; callers separately prove the original transport."""
    _check(identity["role_id"] == "writer" and output_contract == {"name": "AgentTextOutput", "version": "agent"}
           and isinstance(text, str) and len(text.encode("utf-8")) <= 2_000_000)
    try:
        strict_json_loads(text)
    except DomainError as error:
        _check(isinstance(error.__cause__, json.JSONDecodeError))
    else:
        _check(False)
    try:
        start = _value_start(text, "world_ref")
        match = WORLD.match(text, start)
        _check(match is not None)
        keys = [match.group("key" + str(i)) for i in range(3)]
        _check(set(keys) == {"work_id", "branch_id", "world_revision_id"})
        missing = [i for i in range(3) if match.group("close" + str(i)) is None]
        _check(len(missing) == 1 and all(match.group("value" + str(i)) == identity["world_ref"][key]
                                       for i, key in enumerate(keys)))
        offset = match.end("value" + str(missing[0]))
        fixed = text[:offset] + '"' + text[offset:]
        envelope = strict_json_loads(fixed)
        validate_contract("AgentModelResponse", envelope, "agent")
        validate_contract("AgentTextOutput", envelope["payload"], "agent")
        _check(all(envelope[key] == identity[key] for key in FIELDS)
               and envelope["schema_version"] == "2.1" and envelope["status"] == "SUCCEEDED"
               and envelope["unresolved_questions"] == [])
        payload_at = _value_start(fixed, "payload")
        _, payload_end = json.JSONDecoder().raw_decode(fixed, payload_at)
        _check(not payload_at <= offset < payload_end)
        original_at = payload_at - (offset < payload_at)
        original_end = payload_end - (offset < payload_end)
        original_payload = text[original_at:original_end]
        _check(original_payload == fixed[payload_at:payload_end]
               and strict_json_loads(original_payload) == envelope["payload"])
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, DomainError):
        _check(False)
    proof = {"policy": POLICY, "operation": "INSERT_FROZEN_WORLDREF_UUID_CLOSING_QUOTE",
             "offset_codepoints": offset, "inserted_character": '"', "world_ref_field": keys[missing[0]],
             "original_response_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "normalized_response_sha256": hashlib.sha256(fixed.encode()).hexdigest(),
             "original_payload_slice_sha256": hashlib.sha256(original_payload.encode()).hexdigest(),
             "normalized_payload_slice_sha256": hashlib.sha256(fixed[payload_at:payload_end].encode()).hexdigest(),
             "candidate_sha256": fingerprint(envelope["payload"])}
    return envelope, proof


def derive_completed_writer(kernel, repo, task, evidence, response_row):
    """Reproduce the derivation from complete, attempt-bound original HTTP bytes."""
    raw = response_row["payload"]
    work = task["work_id"]
    bound = kernel.artifact(repo, evidence["input_ref"], work, kind="AGENT_INPUT")["payload"]
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    grant = repo.one("grants", {"grant_id": task["grant_id"]})
    config = evidence["model_configuration"]
    metadata = _provider_json(raw["provider_response_json"])
    _check(task["role_id"] == bound["role_id"] == evidence["role_id"] == "writer"
           and spec["role_id"] == task["role_id"] and evidence["input_ref"] in spec["input_artifacts"]
           and grant and evidence["actor_id"] == grant["actor_id"]
           and bound["output_contract"] == {"name": "AgentTextOutput", "version": "agent"}
           and config.get("adapter_kind") == "deepseek" and config.get("execution_kind") == "REAL_EXTERNAL"
           and config["model"] == metadata.get("model") and metadata.get("finish_reason") == "stop"
           and fingerprint(config) == bound["model_config_sha256"]
           and fingerprint(raw) == evidence["response_ref"]["sha256"]
           and fingerprint(bound) == evidence["input_ref"]["sha256"]
           and all(evidence[key] == task[key] for key in ("task_id", "generation", "context_bundle_id", "role_id"))
           and evidence["world_ref"] == spec["world_ref"]
           and raw["request_sha256"] == evidence["request_sha256"]
           and raw["provider_receipt"] == evidence["provider_receipt"])
    wire = read_receipt(kernel.objects, evidence["attempt_id"], evidence["request_sha256"])
    _check(wire is not None)
    receipt, body = wire
    provider = _provider_json(body)
    choice = provider["choices"][0]
    _check(receipt["complete"] is True and receipt["http_status"] == 200
           and provider["model"] == config["model"] and choice["finish_reason"] == "stop"
           and choice["message"]["content"] == raw["text"] and not choice["message"].get("tool_calls")
           and (receipt.get("receipt") or provider["id"]) == evidence["provider_receipt"])
    envelope, proof = derive(raw["text"], evidence, bound["output_contract"])
    proof.update(record_version=1, attempt_id=evidence["attempt_id"], task_id=task["task_id"], generation=task["generation"],
                 source_response_ref=evidence["response_ref"], source_input_ref=evidence["input_ref"],
                 source_request_sha256=evidence["request_sha256"], source_world_ref=spec["world_ref"],
                 provider_calls_sent=0, semantics="STRUCTURAL_DERIVATION_NO_MODEL_OR_WORLD_ACCEPTANCE")
    validate_contract("WriterFormatNormalization", proof, "agent")
    return envelope, proof


def record_normalization(kernel, repo, task, proof):
    validate_contract("WriterFormatNormalization", proof, "agent")
    attempt = UUID(proof["attempt_id"])
    ref = {"artifact_id": str(uuid5(attempt, "writer-format-normalization")),
           "artifact_revision_id": str(uuid5(attempt, "writer-format-normalization:revision")), "sha256": fingerprint(proof)}
    kernel.objects.put(canonical_bytes(proof))
    old = repo.one("artifacts", {"artifact_revision_id": ref["artifact_revision_id"]})
    if old:
        _check(old["kind"] == KIND and old["work_id"] == task["work_id"] and old["sha256"] == ref["sha256"])
    else:
        kernel._register_artifact(repo, ref, task["work_id"], proof, KIND, task["principal_id"])
    return ref


def replay_response(kernel, repo, task, evidence, response_row):
    if not evidence.get("normalization_ref"):
        return strict_json_loads(response_row["payload"]["text"])
    envelope, expected = derive_completed_writer(kernel, repo, task, evidence, response_row)
    ref = evidence["normalization_ref"]
    proof = kernel.artifact(repo, ref, task["work_id"], kind=KIND)["payload"]
    _check(proof == expected and fingerprint(proof) == ref["sha256"]
           and ref["artifact_revision_id"] == str(uuid5(UUID(evidence["attempt_id"]), "writer-format-normalization:revision")))
    return envelope


def result_artifacts(audit_ref, evidence):
    return [audit_ref, evidence["output_ref"], *([evidence["normalization_ref"]] if evidence.get("normalization_ref") else [])]
