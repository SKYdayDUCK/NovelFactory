"""Offline, append-only paired-evaluation evidence with exact original bytes.

This is a research evidence store, not a world database or model executor.
Hash chaining detects changes relative to retained checkpoints; it cannot
authenticate a human, a provider, or a history without an external anchor.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid5

from novel_factory.domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.native_files import file_lock, open_append, publish_file, sync_directory
from novel_factory.infrastructure.private_files import prepare_private_directory
from .contracts import validate
from .protocol import condition_accounting, controls_signature, require, state_for, validate_plan

VERSION = "P3-EVAL-1"
MAX_LEDGER_BYTES = 50_000_000
MAX_EVENTS = 10_000
DIMENSIONS = ["READING_WILLINGNESS", "CHARACTER_CREDIBILITY", "EMOTIONAL_PAYOFF", "NARRATIVE_PACING", "LANGUAGE"]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _refs(value):
    if isinstance(value, dict):
        if set(value) == {"sha256", "byte_length", "media_type"}:
            yield value
        else:
            for item in value.values():
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _immutable_file(path, data):
    """Publish a complete file by hard link, never expose a half-written object."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            publish_file(temporary, path)
        except FileExistsError:
            require(not path.is_symlink() and path.read_bytes() == data,
                    "EVALUATION_OBJECT_CONFLICT", "Existing immutable evidence differs")
            sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


class EvaluationStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.objects = self.root / "objects"

    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root / ".ledger.lock"
        return file_lock(path)

    def put(self, content, media_type="application/json"):
        if isinstance(content, str):
            data = content.encode("utf-8")
        elif isinstance(content, bytes):
            data = content
        else:
            data = canonical_bytes(content)
        ref = validate("ObjectRef", {"sha256": sha256(data).hexdigest(), "byte_length": len(data), "media_type": media_type})
        if media_type != "application/octet-stream":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise DomainError("INVALID_EVALUATION_INPUT", "Evidence text must be exact UTF-8") from None
        _immutable_file(self.objects / ref["sha256"], data)
        return ref

    def raw(self, digest):
        require(isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest),
                "INVALID_EVALUATION_INPUT", "Invalid content hash")
        path = self.objects / digest
        require(path.is_file() and not path.is_symlink(), "EVALUATION_OBJECT_MISSING", "Referenced original evidence is unavailable")
        require(path.stat().st_size <= 20_000_000, "EVALUATION_OBJECT_CORRUPT", "Object exceeds its bounded evidence size")
        data = path.read_bytes()
        require(sha256(data).hexdigest() == digest, "EVALUATION_OBJECT_CORRUPT", "Original evidence hash differs")
        return data

    def read(self, ref):
        validate("ObjectRef", ref)
        data = self.raw(ref["sha256"])
        require(len(data) == ref["byte_length"], "EVALUATION_OBJECT_CORRUPT", "Original evidence byte length differs")
        if ref["media_type"] != "application/octet-stream":
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise DomainError("EVALUATION_OBJECT_CORRUPT", "Stored evidence is not UTF-8") from None
        return data

    def initialize(self, plan):
        plan = validate_plan(plan)
        self._check_objects(plan, [])
        with self._locked():
            _immutable_file(self.root / "plan.json", canonical_bytes(plan) + b"\n")
            ledger = self.root / "ledger.jsonl"
            if not ledger.exists():
                _immutable_file(ledger, b"")
            return self._snapshot_locked()

    def _check_objects(self, plan, events):
        seen = set()
        for ref in _refs([plan, *events]):
            key = (ref["sha256"], ref["byte_length"], ref["media_type"])
            if key not in seen:
                self.read(ref)
                seen.add(key)
        for key in ("model_config_sha256", "permission_manifest_sha256", "prompt_template_sha256"):
            self.raw(plan["controls"][key])
        auxiliary = {ref["sha256"] for condition in plan["conditions"] for ref in condition["material_refs"]}
        # Check the complete retained history before validating any gold row.
        # Otherwise an early gold could become circular when a later candidate
        # reuses that same object, escaping an order-dependent single pass.
        produced = set()
        for observed in events:
            if observed["event_type"] in {"ATTEMPT_FINISHED", "ATTEMPT_RECONCILED"}:
                result = observed["result"]
                if result["response_ref"]:
                    produced.add(result["response_ref"]["sha256"])
                if result["candidate"] is not None:
                    produced.update(ref["sha256"] for ref in _refs(result["candidate"]))
        for event in events:
            if event["event_type"] in {"ATTEMPT_FINISHED", "ATTEMPT_RECONCILED"}:
                result = event["result"]
                if result["response_ref"]:
                    produced.add(result["response_ref"]["sha256"])
                candidate = result["candidate"]
                if candidate is not None:
                    require(candidate["text_ref"]["media_type"] == "text/plain; charset=utf-8"
                            and all(s["media_type"] == "text/plain; charset=utf-8" for s in candidate["ordered_scene_refs"]),
                            "EVALUATION_CANDIDATE_MISSING", "Whole-volume candidates retain exact original UTF-8 scenes")
                    texts = [self.read(ref) for ref in candidate["ordered_scene_refs"]]
                    require(self.read(candidate["text_ref"]) == b"\n\n".join(texts),
                            "EVALUATION_CANDIDATE_MISSING", "Whole volume differs from its complete ordered original scenes")
                    produced.update(ref["sha256"] for ref in _refs(candidate))
            elif event["event_type"] == "GOLD_REGISTERED":
                require(not {ref["sha256"] for ref in event["source_refs"]}.intersection(auxiliary | produced),
                        "EVALUATION_CIRCULAR_GOLD", "Memory, summaries and this experiment's generated outputs cannot be their own gold")
                provenance = validate("GoldProvenance", strict_json_loads(self.read(event["provenance_ref"])))
                require(all(provenance[key] == event[key] for key in ("source_kind", "world_ref", "source_refs", "generated_by_experiment")),
                        "EVALUATION_GOLD_MISMATCH", "Gold source differs from the retained upstream provenance")
            elif event["event_type"] == "ATTEMPT_START_REJECTED":
                attempted = validate("ATTEMPT_STARTED", strict_json_loads(self.read(event["attempted_event_ref"])))
                require(attempted["attempt_id"] == event["attempt_id"] and attempted["event_id"] == event["event_id"],
                        "EVALUATION_LEDGER_CORRUPT", "Rejected request does not match its retained exact start")

    def _snapshot_locked(self, expected_head=None):
        plan_path, ledger = self.root / "plan.json", self.root / "ledger.jsonl"
        require(plan_path.is_file() and ledger.is_file() and not plan_path.is_symlink() and not ledger.is_symlink(),
                "EVALUATION_NOT_INITIALIZED", "Initialize the immutable experiment plan before recording attempts")
        require(plan_path.stat().st_size <= 2_000_000 and ledger.stat().st_size <= MAX_LEDGER_BYTES,
                "EVALUATION_LEDGER_LIMIT", "Evaluation evidence exceeds the supported bounded local ledger")
        plan = validate_plan(strict_json_loads(plan_path.read_bytes()))
        plan_sha = fingerprint(plan)
        head = fingerprint({"protocol": "P3-EVAL-LEDGER-1", "plan_sha256": plan_sha})
        data = ledger.read_bytes()
        require(not data or data.endswith(b"\n"), "EVALUATION_LEDGER_CORRUPT", "Incomplete append retained; recovery requires the original checkpoint")
        lines = data.splitlines()
        require(len(lines) <= MAX_EVENTS, "EVALUATION_LEDGER_LIMIT", "Ledger event capacity reached")
        records, events = [], []
        for sequence, line in enumerate(lines, start=1):
            row = strict_json_loads(line)
            require(isinstance(row, dict) and set(row) == {"protocol", "sequence", "previous_sha256", "event", "sha256"},
                    "EVALUATION_LEDGER_CORRUPT", "Ledger row shape differs")
            body = {key: value for key, value in row.items() if key != "sha256"}
            require(row["protocol"] == "P3-EVAL-LEDGER-1" and row["sequence"] == sequence
                    and row["previous_sha256"] == head and row["sha256"] == fingerprint(body)
                    and canonical_bytes(row) == line,
                    "EVALUATION_LEDGER_CORRUPT", "Ledger sequence or predecessor hash differs")
            events.append(row["event"])
            records.append(row)
            head = row["sha256"]
        state = state_for(plan, events)
        self._check_objects(plan, events)
        require(expected_head is None or expected_head == head, "EVALUATION_CHECKPOINT_MISMATCH", "Current history differs from the retained external checkpoint")
        return {"plan": plan, "plan_sha256": plan_sha, "head_sha256": head,
                "event_count": len(events), "events": events, "records": records, "state": state}

    def snapshot(self, *, expected_head=None):
        with self._locked():
            return self._snapshot_locked(expected_head)

    def checkpoint(self):
        current = self.snapshot()
        return {"protocol": "P3-EVAL-CHECKPOINT-1", "plan_sha256": current["plan_sha256"],
                "event_count": current["event_count"], "head_sha256": current["head_sha256"]}

    def _persist(self, event, current):
        require(current["event_count"] < MAX_EVENTS, "EVALUATION_LEDGER_LIMIT", "Ledger event capacity reached")
        body = {"protocol": "P3-EVAL-LEDGER-1", "sequence": current["event_count"] + 1,
                "previous_sha256": current["head_sha256"], "event": event}
        record = {**body, "sha256": fingerprint(body)}
        data = canonical_bytes(record) + b"\n"
        ledger = self.root / "ledger.jsonl"
        require(ledger.stat().st_size + len(data) <= MAX_LEDGER_BYTES, "EVALUATION_LEDGER_LIMIT", "Ledger byte capacity reached")
        with open_append(ledger) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return record

    def append(self, event):
        event = validate("EvaluationEvent", event)
        require(event["event_type"] not in {"ATTEMPT_START_REJECTED", "BLIND_PACKET_CREATED"},
                "EVALUATION_RESERVED_EVENT", "Rejection and allocation receipts are generated by their services")
        with self._locked():
            current = self._snapshot_locked()
            return self._append_locked(event, current)

    def _append_locked(self, event, current):
        for record in current["records"]:
            previous = record["event"]
            if previous["event_id"] == event["event_id"]:
                if previous == event:
                    return record
                if previous["event_type"] == "ATTEMPT_START_REJECTED" and previous["attempted_event_ref"]["sha256"] == fingerprint(event):
                    raise DomainError(previous["reason_code"], "This rejected start is already retained; no dispatch was authorized")
                raise DomainError("EVALUATION_LEDGER_DUPLICATE", "An event identity cannot be rebound to different evidence")
        events = [*current["events"], event]
        self._check_objects(current["plan"], events)
        try:
            state_for(current["plan"], events)
        except DomainError as exc:
            if event["event_type"] == "ATTEMPT_STARTED" and exc.code in {
                "EVALUATION_BUDGET_EXHAUSTED", "EVALUATION_CONTROLS_MISMATCH", "EVALUATION_ATTEMPT_CONFLICT"
            }:
                # A rejected preflight is retained separately. It is not a
                # dispatched provider call, and does not erase any actual call.
                rejected = {"schema_version": VERSION, "event_id": event["event_id"], "observed_at": event["observed_at"],
                    "event_type": "ATTEMPT_START_REJECTED", "attempt_id": event["attempt_id"],
                    "attempted_event_ref": self.put(event), "reason_code": exc.code}
                validate("EvaluationEvent", rejected)
                self._persist(rejected, current)
            raise
        return self._persist(event, current)

    def _selected(self, current):
        plan, state = current["plan"], current["state"]
        selected = [aid for aid, decision in state["decisions"].items() if decision["disposition"] == "SELECTED"]
        require(len(selected) == 2 and len({state["attempts"][aid]["condition_id"] for aid in selected}) == 2,
                "EVALUATION_PAIR_INCOMPLETE", "Select exactly one complete original per paired condition")
        require(not any(row["cap_breached"] for row in condition_accounting(plan, state)),
                "EVALUATION_BUDGET_EXHAUSTED", "Observed costs or usage exceed the frozen paired budget")
        output = {}
        bounds = plan["controls"]
        for aid in selected:
            attempt = state["attempts"][aid]
            result = attempt.get("result")
            require(result is not None and result["outcome"] == "SUCCEEDED" and result["candidate"] is not None,
                    "EVALUATION_PAIR_INCOMPLETE", "Only a completed retained candidate can enter a blind pair")
            candidate = result["candidate"]
            text = self.read(candidate["text_ref"])
            count, length = len(candidate["ordered_scene_refs"]), len(text.decode("utf-8"))
            require(bounds["scene_count_min"] <= count <= bounds["scene_count_max"]
                    and bounds["target_codepoints_min"] <= length <= bounds["target_codepoints_max"],
                    "EVALUATION_TARGET_MISMATCH", "Selected whole-volume unit is outside the fixed length or scene bounds")
            output[attempt["condition_id"]] = {"attempt_id": aid, "candidate": candidate,
                "text": text, "codepoints": length, "scene_count": count, "execution_kind": attempt["execution_kind"]}
        return output

    def report(self):
        current = self.snapshot()
        plan, state = current["plan"], current["state"]
        try:
            self._selected(current)
            readiness, preparation_blocker = "READY_FOR_REVIEW", None
        except DomainError as exc:
            readiness, preparation_blocker = "BLOCKED_PAIR_PREPARATION", exc.code
        attempts = list(state["attempts"].values())
        blockers = ["REAL_MODEL_PROVENANCE_NOT_INDEPENDENTLY_AUTHENTICATED",
                    "HUMAN_IDENTITY_AND_FULL_READING_NOT_INDEPENDENTLY_AUTHENTICATED",
                    "NO_AUTOMATIC_LITERARY_WINNER_POLICY"]
        if not any(a["execution_kind"] == "REAL_MODEL_DECLARED" for a in attempts):
            blockers.append("NO_REAL_MODEL_PAIR")
        if not any(g["purpose"] == "LITERARY_READING" and g["source_kind"] == "EXTERNAL_HUMAN_ANNOTATION" for g in state["gold"]):
            blockers.append("NO_EXTERNAL_HUMAN_LITERARY_GOLD")
        if not any(r["origin"] == "EXTERNAL_HUMAN_INPUT_UNVERIFIED" and r["full_original_reading_claim"] for r in state["readings"]):
            blockers.append("NO_EXTERNAL_HUMAN_FULL_READING_DECLARATION")
        accounting = condition_accounting(plan, state)
        if any(row["unknown_cost_attempts"] for row in accounting):
            blockers.append("ACTUAL_COST_REMAINS_UNKNOWN")
        return {"schema_version": VERSION, "experiment_id": plan["experiment_id"], "sample_id": plan["sample_id"],
            "unit": plan["unit"], "factor": plan["factor"], "controls_sha256": controls_signature(plan),
            "plan_sha256": current["plan_sha256"], "event_count": current["event_count"], "ledger_head_sha256": current["head_sha256"],
            "integrity_status": "LOCAL_HASH_CHAIN_AND_OBJECTS_VERIFIED", "history_completeness": "REQUIRES_EXTERNAL_CHECKPOINT",
            "readiness": readiness, "preparation_blocker": preparation_blocker,
            "literary_inference_status": "BLOCKED_LITERARY_INFERENCE", "literary_inference_blockers": blockers,
            "comparability_scope": "FROZEN_DECLARED_PROTOCOL_AND_RETAINED_FILES;EXECUTION_IDENTITY_NOT_AUTHENTICATED",
            "execution_counts": {kind: sum(a["execution_kind"] == kind for a in attempts)
                for kind in ("REAL_MODEL_DECLARED", "ENGINEERING_FIXTURE", "MANUAL_CANDIDATE")},
            "provider_calls_made_by_evaluation_tool": 0, "condition_accounting": accounting,
            "attempts": [{"attempt_id": a["attempt_id"], "condition_id": a["condition_id"], "execution_kind": a["execution_kind"],
                "outcome": a["result"]["outcome"] if a.get("result") else "IN_FLIGHT",
                "observed_outcomes": [e["result"]["outcome"] for e in a["results"]],
                "candidate_ref": a.get("result", {}).get("candidate"), "decision": state["decisions"].get(a["attempt_id"]),
                "actual_cost_usd": a.get("result", {}).get("actual_cost_usd"),
                "latency_ms": a.get("result", {}).get("latency_ms")} for a in attempts],
            "rejected_start_count": len(state["rejected_starts"]), "human_intervention_count": len(state["interventions"]),
            "gold_sources": state["gold"], "reading_declarations": state["readings"], "blind_packet_count": len(state["blind_packets"])}

    def blind(self, reviewer_directory, coordinator_directory):
        reviewer, coordinator = Path(reviewer_directory).resolve(), Path(coordinator_directory).resolve()
        root = self.root.resolve()
        require(reviewer != coordinator and not reviewer.is_relative_to(coordinator)
                and not coordinator.is_relative_to(reviewer) and reviewer != root
                and not reviewer.is_relative_to(root) and not root.is_relative_to(reviewer),
                "EVALUATION_BLINDING_LEAK", "Reviewer packet, coordinator key and experiment ledger must be separate directories")
        with self._locked():
            current = self._snapshot_locked()
            selected = self._selected(current)
            plan = current["plan"]
            selected_ids = sorted(v["attempt_id"] for v in selected.values())
            existing = next((p for p in reversed(current["state"]["blind_packets"])
                             if sorted(p["selected_attempt_ids"]) == selected_ids), None)
            if existing:
                packet_ref, allocation_ref = existing["packet_ref"], existing["allocation_ref"]
                packet, allocation = strict_json_loads(self.read(packet_ref)), strict_json_loads(self.read(allocation_ref))
            else:
                # Precommitted seed and sample identity, independent of response
                # arrival order. This is reproducible allocation, not a claim
                # of malicious-coordinator resistance or Host isolation.
                ordered = sorted(selected, key=lambda cid: fingerprint({"seed": plan["randomization_seed"], "sample_id": plan["sample_id"], "condition_id": cid}))
                packet_id = str(uuid5(UUID(plan["experiment_id"]), "blind:" + fingerprint(selected_ids)))
                versions, mapping = [], []
                for label, cid in zip(("A", "B"), ordered, strict=True):
                    value = selected[cid]
                    versions.append({"label": label, "file": label + ".txt", "sha256": value["candidate"]["text_ref"]["sha256"],
                        "byte_length": len(value["text"]), "codepoints": value["codepoints"], "scene_count": value["scene_count"]})
                    mapping.append({"label": label, "condition_id": cid, "attempt_id": value["attempt_id"],
                                    "candidate_sha256": value["candidate"]["text_ref"]["sha256"]})
                brief = self.read(plan["controls"]["creative_brief_ref"])
                require(plan["controls"]["creative_brief_ref"]["media_type"] == "text/plain; charset=utf-8",
                        "EVALUATION_BLINDING_LEAK", "Reviewer creative brief must be separately prepared plain text")
                identifiers = [plan["experiment_id"], plan["sample_id"], *selected]
                require(not any(identity.encode() in content for identity in identifiers
                                for content in [brief, *[s["text"] for s in selected.values()]]),
                        "EVALUATION_BLINDING_LEAK", "Review text or brief directly exposes a condition or experiment identity")
                packet = {"protocol": "P3-BLIND-1", "packet_id": packet_id, "status": "READY_FOR_REVIEW", "unit": plan["unit"],
                    "brief": {"file": "brief.txt", "sha256": sha256(brief).hexdigest(), "byte_length": len(brief)},
                    "versions": versions, "dimensions": DIMENSIONS,
                    "instructions": "Read both complete originals under the shared brief. Record observations with exact quotations and version labels. Mixed feelings and evolving voices are valid. No universal beauty score or automatic winner is required."}
                allocation = {"protocol": "P3-BLIND-KEY-1", "packet_id": packet_id,
                    "algorithm": "SHA256_SEEDED_CONDITION_ORDER_V1", "seed": plan["randomization_seed"],
                    "experiment_id": plan["experiment_id"], "sample_id": plan["sample_id"],
                    "plan_sha256": current["plan_sha256"], "source_ledger_head": current["head_sha256"],
                    "conditions": plan["conditions"], "mapping": mapping,
                    "delivery_status": "PREPARED_NOT_DELIVERED;REVIEWER_ISOLATION_NOT_VERIFIED"}
                packet_ref, allocation_ref = self.put(packet), self.put(allocation)
                event = {"schema_version": VERSION, "event_id": str(uuid5(UUID(packet_id), "allocation-record")),
                    "observed_at": _now(), "event_type": "BLIND_PACKET_CREATED", "packet_ref": packet_ref,
                    "allocation_ref": allocation_ref, "source_ledger_head": current["head_sha256"], "selected_attempt_ids": selected_ids}
                self._append_locked(event, current)
            files = {v["file"]: self.raw(v["sha256"]) for v in packet["versions"]}
            files["brief.txt"] = self.raw(packet["brief"]["sha256"])
            files["review.json"] = canonical_bytes(packet) + b"\n"
            files["README.txt"] = (
                "Read brief.txt and all of A.txt and B.txt. Return qualitative observations with source quotations.\n"
                "This packet offers exact originals; it does not record that anyone has read them.\n"
                "Only deliver this directory to a reviewer. Keep experiment records and the coordinator key elsewhere.\n"
                "Text may itself reveal a condition; independent review of blinding is still required.\n"
            ).encode()
            require(not reviewer.exists() or {p.name for p in reviewer.iterdir()} <= set(files),
                    "EVALUATION_BLINDING_LEAK", "Reviewer directory contains files outside the blind packet")
            for name, content in files.items():
                _immutable_file(reviewer / name, content)
            prepare_private_directory(coordinator)
            _immutable_file(coordinator / "allocation.json", canonical_bytes(allocation) + b"\n")
            return {"packet_ref": packet_ref, "allocation_ref": allocation_ref, "reviewer_directory": str(reviewer),
                    "coordinator_directory": str(coordinator), "delivery_status": allocation["delivery_status"]}
