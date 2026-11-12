"""Independent-review receipts. NF keeps task authority; external subject identity stays at its source."""
import asyncio
import hmac
import json
import os
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.narrative import NarrativeService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError


PROOF_KIND = "CREATIVE_REVIEW_INDEPENDENCE"


def _check(value):
    require(value, "INDEPENDENT_REVIEW_REQUIRED", "Live, qualified independent review evidence is required")


def _private_bytes(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        meta = os.fstat(fd)
        _check(stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid()
               and not meta.st_mode & 0o077 and 0 < meta.st_size <= 65536)
        return source.read(65537)


def native_case_path(binding):
    root = Path(os.environ["NF_REVIEW_NATIVE_ATTESTATIONS"])
    meta = root.lstat()
    _check(stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid() and not meta.st_mode & 0o077)
    return root / (fingerprint(binding) + ".json")


def native_signature(body):
    key = _private_bytes(os.environ["NF_REVIEW_NATIVE_KEY"])
    _check(len(key) >= 32)
    return hmac.digest(key, b"nf.native-review-case.v1\0" + canonical_bytes(body), "sha256").hex()


def validate_native_case(binding, case):
    _check(case["binding"] == binding and case["version"] == "nf.native-review-case.v1")
    expires = datetime.fromisoformat(case["expires_at"].replace("Z", "+00:00"))
    _check(expires.tzinfo is not None and expires > datetime.now(UTC))
    subjects = case["subjects"]
    owners = {p["principal_id"] for p in binding["producers"]}
    reviewer = binding["reviewer"]["principal_id"]
    _check(set(subjects) == owners | {reviewer})
    for record in subjects.values():
        # The offline registrar supplies the digest of the evidence actually inspected.
        _check(set(record) == {"group", "evidence_sha256"} and isinstance(record["group"], str)
               and record["group"].strip() == record["group"] and 0 < len(record["group"]) <= 200
               and len(record["evidence_sha256"]) == 64
               and all(ch in "0123456789abcdef" for ch in record["evidence_sha256"]))
    _check(subjects[reviewer]["group"] not in {subjects[p]["group"] for p in owners})
    return expires


async def _workshop_reads(bindings):
    # A volume shares one authenticated MCP process. Bound database fan-out across
    # concurrent RPCs; each handler authenticates the binding again in its own transaction.
    params = StdioServerParameters(command=os.environ.get("NF_REVIEW_WORKSHOP_EXECUTABLE", "aw-mcp"),
                                 args=["--binding", os.environ["NF_REVIEW_WORKSHOP_BINDING"]])
    async with asyncio.timeout(10):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            window = asyncio.Semaphore(8)

            async def read_one(binding):
                async with window:
                    started = monotonic()
                    result = await client.call_tool("aw_review_independence", {"binding": binding})
                    _check(not result.isError)
                    evidence = result.structuredContent
                    _check(evidence and evidence["binding_sha256"] == fingerprint(binding))
                    return _LiveProof(fingerprint(evidence), fingerprint(binding),
                                      datetime.fromisoformat(evidence["expires_at"]), started)

            return await asyncio.gather(*(read_one(b) for b in bindings))


@dataclass(frozen=True)
class _LiveProof:
    evidence_digest: str
    binding_digest: str
    valid_until: datetime
    checked_at: float


def _refresh_bindings(bindings):
    try:
        result = {}
        remote = [b for b in bindings if b["authority"] == "WORKSHOP"]
        if remote:
            snapshots = asyncio.run(_workshop_reads(remote))
            result.update((s.binding_digest, s) for s in snapshots)
        for binding in bindings:
            if binding["authority"] != "NATIVE":
                continue
            checked = monotonic()
            envelope = json.loads(_private_bytes(native_case_path(binding)))
            evidence = envelope["case"]
            _check(hmac.compare_digest(native_signature(evidence), envelope["signature"]))
            expires = validate_native_case(binding, evidence)
            digest = fingerprint(binding)
            result[digest] = _LiveProof(fingerprint(evidence), digest, expires, checked)
        _check(all(fingerprint(b) in result for b in bindings))
        return result
    except DomainError:
        raise
    except Exception:
        _check(False)


class ReviewIndependenceService:
    def __init__(self, kernel):
        self.kernel = kernel

    def _assignment(self, repo, task, generation):
        task = repo.one("tasks", {"task_id": task["task_id"]}, lock="share")
        _check(task and task["generation"] == generation and task["state"] not in {"CANCELLED", "FAILED", "STALE"})
        owner = Principal(task["grant_id"], task["principal_id"])
        grant = self.kernel.authorize(repo, owner, task["work_id"], "READ", task["branch_id"])
        cut = repo.one("contexts", {"task_id": task["task_id"], "generation": generation,
                                   "context_bundle_id": task["context_bundle_id"], "grant_id": grant["grant_id"]})
        _check(cut and cut["read_cut"]["permission_epoch"] == grant["epoch"])
        identity = {"principal_id": owner.principal_id, "grant_id": owner.grant_id, "permission_epoch": grant["epoch"]}
        receipt = repo.one("delegation_receipts", {"task_id": task["task_id"]}, lock="share")
        delegation = None
        if receipt:
            installation = repo.one("delegation_installations", {"installation_id": receipt["installation_id"]}, lock="share")
            slot = repo.one("delegation_slots", {"installation_id": receipt["installation_id"],
                                                 "scope_ref": receipt["scope_ref"]}, lock="share")
            work = repo.one("works", {"work_id": task["work_id"]})
            _check(installation and installation["active"] and not installation["outbound_fenced"]
                   and installation["expires_at"] > repo.now() and installation["policy_version"] == work["policy_version"]
                   and (installation["work_id"], installation["branch_id"]) == (task["work_id"], task["branch_id"])
                   and slot and (slot["principal_id"], slot["grant_id"], slot["grant_epoch"])
                   == (owner.principal_id, owner.grant_id, grant["epoch"]))
            delegation = {k: installation[k] for k in ("installation_id", "org_id", "nf_instance_id", "epoch")}
            delegation.update({k: receipt[k] for k in ("operation_id", "receipt_ref", "scope_ref")})
        return {**identity, "task_id": task["task_id"], "generation": generation, "delegation": delegation}

    def _binding(self, repo, manuscript_ref, reviewer_task_id, reviewer_generation, controller=None, *, allow_accepted=False):
        task = repo.one("tasks", {"task_id": reviewer_task_id}, lock="share")
        _check(task and task["role_id"] in {"reviewer", "chief_editor", "literary_editor"})
        work, branch = task["work_id"], task["branch_id"]
        if controller is not None:
            self.kernel.authorize(repo, controller, work, "ADMIN", branch)
        reviewer = self._assignment(repo, task, reviewer_generation)
        owner = Principal(task["grant_id"], task["principal_id"])
        narrative = NarrativeService(self.kernel)
        _, _, _, draft = narrative._reader_binding(repo, owner, reviewer_task_id, reviewer_generation, manuscript_ref, allow_accepted=allow_accepted)
        seen, producers, source = set(), [], manuscript_ref
        while source:
            _check(source["artifact_revision_id"] not in seen)
            seen.add(source["artifact_revision_id"])
            row = self.kernel.artifact(repo, source, work, kind="NARRATIVE_DRAFT")
            record = row["payload"]
            _check(fingerprint(record) == source["sha256"] and record["input_world_ref"]["branch_id"] == branch)
            writer, _, _, _ = narrative._origin(repo, record, live=False)
            _check(writer["principal_id"] == row["created_by"]
                   and writer["work_id"] == work and writer["branch_id"] == branch)
            participant = self._assignment(repo, writer, record["generation"])
            if participant not in producers:
                producers.append(participant)
            source = record["prior_draft_ref"]
        _check(reviewer["principal_id"] not in {p["principal_id"] for p in producers})
        if controller is not None:
            _check(controller.principal_id not in {p["principal_id"] for p in [*producers, reviewer]})
        delegations = [p["delegation"] for p in [*producers, reviewer]]
        _check(all(d is None for d in delegations) or all(d and d["installation_id"] == delegations[0]["installation_id"]
                                                         for d in delegations))
        return {"authority": "WORKSHOP" if reviewer["delegation"] else "NATIVE", "world_ref": draft["input_world_ref"],
                "manuscript_ref": manuscript_ref, "producers": producers, "reviewer": reviewer}

    def describe(self, controller, *, manuscript_ref, reviewer_task_id, reviewer_generation):
        with self.kernel.db.uow(write=False) as repo:
            return self._binding(repo, manuscript_ref, reviewer_task_id, reviewer_generation, controller, allow_accepted=True)

    def issue(self, controller, *, manuscript_ref, reviewer_task_id, reviewer_generation):
        args = (manuscript_ref, reviewer_task_id, reviewer_generation)
        with self.kernel.db.uow(write=False) as repo:
            binding = self._binding(repo, *args, controller)
            issuer = self.kernel.authorize(repo, controller, binding["world_ref"]["work_id"], "ADMIN", binding["world_ref"]["branch_id"])
        proof = _refresh_bindings([binding])[fingerprint(binding)]
        payload = {"scope": {"world_ref": binding["world_ref"], "task_id": reviewer_task_id, "generation": reviewer_generation},
                   "binding": binding, "evidence_digest": proof.evidence_digest,
                   "issuer_grant_id": controller.grant_id, "issuer_principal_id": controller.principal_id, "issuer_epoch": issuer["epoch"]}
        revision = str(uuid5(NAMESPACE_URL, "nf:review-independence:" + fingerprint(payload)))
        work = binding["world_ref"]["work_id"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work}, lock="update")
            _check(self._binding(repo, *args, controller) == binding
                   and repo.one("grants", {"grant_id": controller.grant_id})["epoch"] == payload["issuer_epoch"])
            _check(proof.valid_until > repo.now() and monotonic() - proof.checked_at <= 10)
            row = repo.one("artifacts", {"work_id": work, "kind": PROOF_KIND, "artifact_revision_id": revision})
            if row:
                _check(row["payload"] == payload)
                ref = {k: row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}
            else:
                ref = self.kernel._blob(payload)
                ref["artifact_revision_id"] = revision
                self.kernel._register_artifact(repo, ref, work, payload, PROOF_KIND, controller.principal_id)
            return {"independence_ref": ref}

    def refresh(self, proof_ref):
        return self.refresh_many([proof_ref])[proof_ref["artifact_revision_id"]]

    def refresh_many(self, refs):
        _check(isinstance(refs, list) and 1 <= len(refs) <= 128)
        requested = {}
        for ref in refs:
            validate_contract("ArtifactRef", ref)
            revision = ref["artifact_revision_id"]
            _check(revision not in requested or requested[revision] == ref)
            requested[revision] = ref
        with self.kernel.db.uow(write=False) as repo:
            rows = repo.many("artifacts", {"kind": PROOF_KIND, "artifact_revision_id": list(requested)})
            _check(len(rows) == len(requested))
            bindings = {}
            for row in rows:
                revision = row["artifact_revision_id"]
                ref = requested[revision]
                _check(all(row[k] == v for k, v in ref.items()) and fingerprint(row["payload"]) == ref["sha256"])
                bindings[revision] = row["payload"]["binding"]
        unique = {fingerprint(value): value for value in bindings.values()}
        live = _refresh_bindings(list(unique.values()))
        return {revision: live[fingerprint(bindings[revision])] for revision in requested}

    def verify(self, repo, *, proof_ref, manuscript_ref, creator_task_id, creator_generation,
               reviewer_task_id, reviewer_generation, live_proof):
        binding = self._binding(repo, manuscript_ref, reviewer_task_id, reviewer_generation, allow_accepted=True)
        producer, reviewer = binding["producers"][0], binding["reviewer"]
        world = binding["world_ref"]
        row = self.kernel.artifact(repo, proof_ref, world["work_id"], kind=PROOF_KIND)
        payload = row["payload"]
        _check((producer["task_id"], producer["generation"]) == (creator_task_id, creator_generation)
               and payload["binding"] == binding and fingerprint(payload) == proof_ref["sha256"]
               and isinstance(live_proof, _LiveProof) and live_proof.binding_digest == fingerprint(binding)
               and live_proof.evidence_digest == payload["evidence_digest"]
               and live_proof.valid_until > repo.now() and 0 <= monotonic() - live_proof.checked_at <= 10)
        issuer = self.kernel.authorize(repo, Principal(payload["issuer_grant_id"], payload["issuer_principal_id"]),
                                       world["work_id"], "ADMIN", world["branch_id"])
        _check(issuer["epoch"] == payload["issuer_epoch"])
        return {"producer": {k: producer[k] for k in ("principal_id", "grant_id", "permission_epoch")},
                "reviewer": {k: reviewer[k] for k in ("principal_id", "grant_id", "permission_epoch")},
                "independence_receipt": proof_ref}
