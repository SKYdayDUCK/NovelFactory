"""Bounded, recoverable world-index consumer of the existing PostgreSQL outbox.

This trusted server worker is not an Agent endpoint. It materializes an immutable
timeline/relationship/evidence/invalidation bundle from one WorldRef. Authority
continues to reside in revisions and scope guards; a derived index never writes
world facts, tasks, permissions, canon selection or published text.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid4, uuid5

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.temporal import point_key
from novel_factory.infrastructure.database import tables
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.tasks import MAINTENANCE_GATE, OutboxService

PROJECTOR_VERSION = "nf-derived-world-1"
CONSUMER = "derived-world:" + PROJECTOR_VERSION
INDEX_NAMES = ("graph", "timeline")


# Both the project dispatcher and standalone durable outbox consumer draw from
# these exact live leases whenever the operator configures resource limits.
_ACTIVE_INDEX_BUDGET = """
                WITH active AS MATERIALIZED (
                    SELECT t.work_id,t.role_id,'MODEL' AS resource FROM nf_dispatch_slots s JOIN nf_tasks t USING(task_id)
                    WHERE s.expires_at>statement_timestamp() UNION ALL
                    SELECT work_id,'derived_index','CPU_COMPUTE' FROM nf_index_leases WHERE expires_at>statement_timestamp()
                )
"""
_INDEX_LIMITS = """
                  AND NOT EXISTS (SELECT FROM nf_dispatch_limits lim WHERE lim.scope='RESOURCE'
                    AND lim.subject='CPU_COMPUTE' AND lim.slots <=
                        (SELECT count(*) FROM active WHERE resource='CPU_COMPUTE'))
                  AND (SELECT count(*) FROM active)<(SELECT slots FROM nf_dispatch_limits WHERE scope='GLOBAL' AND subject='*')
                  AND (SELECT count(*) FROM active WHERE work_id=b.work_id)<(SELECT slots FROM nf_dispatch_limits
                    WHERE scope='WORK' AND subject IN ('*',b.work_id::text) ORDER BY subject='*' LIMIT 1)
                  AND (SELECT count(*) FROM active WHERE role_id='derived_index')<(SELECT slots FROM nf_dispatch_limits
                    WHERE scope='ROLE' AND subject IN ('*','derived_index') ORDER BY subject='*' LIMIT 1)
"""


class DerivedWorldWorker:
    def __init__(self, kernel):
        self.kernel = kernel
        self.outbox = OutboxService(kernel.db.engine, tables)

    def _material(self, lease):
        kernel = self.kernel
        with kernel.db.uow(write=False, repeatable_read=True) as repo:
            message = repo.one("outbox", {"outbox_id": lease["outbox_id"]})
            require(message and message["topic"] == "world.accepted", "INVALID_ARGUMENT",
                    "Derived worker accepts only persisted world.accepted messages")
            wr = validate_contract("WorldRef", message["payload"]["world_ref"])
            revision = kernel.revision(repo, wr)
            require(message["aggregate_type"] == "revision"
                    and message["aggregate_id"] == wr["world_revision_id"],
                    "INVALID_EVIDENCE", "Outbox aggregate differs from its immutable world revision")
            invalidations = repo.many("invalidations", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                                                        "world_revision_id": wr["world_revision_id"]})
            # Later plan/release selections can append invalidations referring
            # to the same WorldRef. Only scope versions covered by this immutable
            # world revision belong in this reproducible index generation.
            invalidations = [row for row in invalidations
                             if row["version"] <= revision["scope_versions"].get(row["scope_key"], -1)]
        payload = {
            "projector_version": PROJECTOR_VERSION, "world_ref": wr,
            "branch_seq": revision["branch_seq"], "source_manifest_sha256": revision["manifest_hash"],
            "source_state_sha256": revision["state_hash"], "interpretation_ref": revision["interpretation_ref"],
            "timeline": sorted(deepcopy(revision["snapshot"]["events"]), key=fingerprint),
            "relations": sorted(deepcopy(revision["snapshot"]["relations"]), key=fingerprint),
            # Cognitions remain classified records; a worker never promotes
            # these into objective world truth or exposes an unfiltered API.
            "cognitions": sorted(deepcopy(revision["snapshot"]["cognitions"]), key=fingerprint),
            "resource_history": sorted(deepcopy(revision["snapshot"]["resource_history"]), key=fingerprint),
            "lifecycle_history": sorted(deepcopy(revision["snapshot"]["lifecycle_history"]), key=fingerprint),
            "frontiers": deepcopy(revision["frontiers"]),
            "invalidations": sorted([
                {key: row[key] for key in ("invalidation_id", "scope_key", "version")}
                for row in invalidations], key=lambda row: row["invalidation_id"]),
            "scope_versions": deepcopy(revision["scope_versions"]),
            "authority": "DERIVED_ONLY_REAUTHORIZE_AT_READ",
        }
        digest = fingerprint(payload)
        ref = {"artifact_id": str(uuid5(UUID(wr["branch_id"]), PROJECTOR_VERSION)),
               "artifact_revision_id": str(uuid5(UUID(wr["world_revision_id"]), PROJECTOR_VERSION)),
               "sha256": digest}
        return payload, ref

    def process_claimed(self, lease, worker_id, *, fault=None):
        """Prepare outside the transaction, then publish with transactional inbox.

        A worker crash can leave an unreferenced content-addressed object. It
        cannot expose a watermark without its durable artifact, acknowledge an
        obsolete lease, or apply a second world effect on redelivery.
        """
        require(bool(worker_id), "INVALID_ARGUMENT", "A delivery lease owner is required")
        return self._materialize(lease, worker_id=worker_id, fault=fault)

    def rebuild_one(self, principal, work_id, branch_id):
        """Repair one immutable index missing its durable receipt, even after legacy delivery."""
        lease = self.reserve_index(principal, work_id, branch_id)
        return self.process_reserved(principal, work_id, branch_id, lease) if lease else None

    def reserve_index(self, principal, work_id, branch_id):
        scope = {"work": work_id, "branch": branch_id, "consumer": CONSUMER,
                 "gate": MAINTENANCE_GATE + 1, "token": str(uuid4()), "work_text": work_id, "branch_text": branch_id}
        with self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            repo.conn.execute(text("SELECT pg_advisory_xact_lock(:gate)"), scope)
            repo.conn.execute(text("LOCK TABLE nf_dispatch_limits IN SHARE MODE"))
            event = repo.conn.scalar(text(_ACTIVE_INDEX_BUDGET + """
                SELECT o.outbox_id FROM nf_outbox o
                JOIN nf_redis_branches b ON b.branch_id=:branch AND b.work_id=:work
                WHERE o.topic='world.accepted'
            """ + _INDEX_LIMITS + """
                  AND o.payload->'world_ref'->>'work_id'=:work_text AND o.payload->'world_ref'->>'branch_id'=:branch_text
                  AND NOT EXISTS (SELECT FROM nf_index_leases l WHERE l.event_id=o.outbox_id AND l.expires_at>now())
                  AND NOT EXISTS (SELECT FROM nf_inbox i WHERE i.consumer=:consumer AND i.message_id=o.outbox_id)
                ORDER BY o.created_at,o.outbox_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED
            """), scope)
            if event is None:
                return None
            repo.conn.execute(text("""
                INSERT INTO nf_index_leases(event_id,token,work_id,branch_id,expires_at)
                VALUES(:id,:token,:work,:branch,now()+interval '120 seconds')
                ON CONFLICT(event_id) DO UPDATE SET token=EXCLUDED.token,expires_at=EXCLUDED.expires_at
            """), {**scope, "id": event})
        return {"outbox_id": str(event), "token": scope["token"]}

    def process_reserved(self, principal, work_id, branch_id, lease):
        try:
            return self._materialize(lease, scope=(principal, work_id, branch_id))
        finally:
            with self.kernel.db.uow() as repo:
                repo.conn.execute(text("DELETE FROM nf_index_leases WHERE event_id=:outbox_id AND token=:token"), lease)

    def _materialize(self, lease, *, worker_id=None, scope=None, fault=None):
        kernel = self.kernel
        payload, ref = self._material(lease)
        stored = kernel.objects.put(canonical_bytes(payload))
        require(stored.sha256 == ref["sha256"], "INVALID_EVIDENCE", "Derived content hash differs")
        if fault:
            fault("after_object_before_consume")
        status = {"materialization": "DUPLICATE", "activated": False}

        def handler(conn, persisted_payload):
            repo = KernelRepository(conn, tables)
            row = repo.one("outbox", {"outbox_id": lease["outbox_id"]}, lock="update")
            if scope is None:
                require(row and row["state"] == "DELIVERING" and row["generation"] == lease["generation"]
                        and row["lease_owner"] == worker_id and row["lease_expiry"] > repo.now(),
                        "LEASE_EXPIRED", "Derived worker delivery lease is stale")
            else:
                principal, work_id, branch_id = scope
                valid = repo.conn.scalar(text("""SELECT event_id FROM nf_index_leases
                    WHERE event_id=:outbox_id AND token=:token AND expires_at>clock_timestamp() FOR UPDATE"""), lease)
                require(valid, "LEASE_EXPIRED", "Index preparation lease expired")
                self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
                require(payload["world_ref"]["work_id"] == work_id and payload["world_ref"]["branch_id"] == branch_id,
                        "VIEW_DENIED", "Derived source outside project")
            wr = payload["world_ref"]
            require(row["topic"] == "world.accepted" and persisted_payload["world_ref"] == wr,
                    "INVALID_EVIDENCE", "Outbox source changed after material preparation")
            if fault:
                fault("before_work_lock")
            # Artifact insertion takes a work FK key-share lock implicitly.
            # Acquire work share before branch update, matching normal world
            # operations; runtime/canon changes take work update then branch.
            # Reversing these could deadlock against an administrative change.
            require(repo.one("works", {"work_id": wr["work_id"]}, lock="share"),
                    "INVALID_EVIDENCE", "Source work is unavailable")
            # Lock only this branch, in the same direction as world accepts.
            # A candidate's sequence number never competes with canon's.
            branch = repo.one("branches", {"work_id": wr["work_id"], "branch_id": wr["branch_id"]}, lock="update")
            require(branch, "INVALID_EVIDENCE", "Source branch is unavailable")
            if fault:
                fault("after_branch_lock")
            source = repo.one("revisions", wr)
            require(source and source["branch_seq"] == payload["branch_seq"]
                    and source["state_hash"] == payload["source_state_sha256"]
                    and source["manifest_hash"] == payload["source_manifest_sha256"],
                    "INVALID_EVIDENCE", "Derived material no longer matches its immutable source")
            previous = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": ref["artifact_revision_id"]})
            if previous:
                require(previous["kind"] == "DERIVED_WORLD_INDEX" and previous["sha256"] == ref["sha256"]
                        and previous["payload"] == payload,
                        "PROJECTION_CORRUPT", "One projection identity has conflicting material")
            else:
                kernel._register_artifact(repo, ref, wr["work_id"], payload, "DERIVED_WORLD_INDEX",
                                          str(uuid5(UUID(wr["work_id"]), CONSUMER)))
            if fault:
                fault("after_artifact_before_watermark")
            activated = False
            for name in INDEX_NAMES:
                old = repo.one("index_watermarks", {"branch_id": wr["branch_id"], "index_name": name})
                if old and old["branch_seq"] > source["branch_seq"]:
                    continue
                if old and old["branch_seq"] == source["branch_seq"]:
                    require(old["world_revision_id"] == wr["world_revision_id"], "PROJECTION_CORRUPT",
                            "The same branch sequence cannot identify two world revisions")
                    continue
                value = {"world_revision_id": wr["world_revision_id"], "branch_seq": source["branch_seq"],
                         "generation": 1 if old is None else old["generation"] + 1}
                if old:
                    repo.change("index_watermarks", {"branch_id": wr["branch_id"], "index_name": name}, value)
                else:
                    repo.add("index_watermarks", {"branch_id": wr["branch_id"], "index_name": name, **value})
                activated = True
            status.update(materialization="STORED", activated=activated)
            if fault:
                fault("after_watermark_before_commit")

        with kernel.objects.lock():
            consumed = self.outbox.consume(CONSUMER, lease["outbox_id"], handler)
        if fault:
            fault("after_consume_before_ack")
        if scope is None:
            self.outbox.ack(lease["outbox_id"], lease["generation"], worker_id)
        return {"message_id": lease["outbox_id"], "consumed": consumed, "artifact_ref": ref,
                "world_ref": payload["world_ref"], **status}

    def run_once(self, worker_id, *, lease_seconds=30, fault=None):
        token = str(uuid4())
        with self.kernel.db.uow() as repo:
            # Lock order matches project index/model admissions: advisory gate,
            # configuration, outbox row, lease. No IO or indexing under these locks.
            require(bool(worker_id) and 0 < lease_seconds <= 3600, "INVALID_ARGUMENT", "Invalid outbox lease")
            repo.conn.execute(text("SELECT pg_advisory_xact_lock(:gate)"), {"gate": MAINTENANCE_GATE + 1})
            repo.conn.execute(text("LOCK TABLE nf_dispatch_limits IN SHARE MODE"))
            scoped = repo.conn.scalar(text("SELECT EXISTS (SELECT FROM nf_dispatch_limits WHERE scope='RESOURCE')"))
            message_id = None
            if scoped:
                message_id = repo.conn.scalar(text(_ACTIVE_INDEX_BUDGET + """
                    SELECT o.outbox_id FROM nf_outbox o JOIN nf_branches b
                      ON b.branch_id::text=o.payload->'world_ref'->>'branch_id'
                     AND b.work_id::text=o.payload->'world_ref'->>'work_id'
                    WHERE o.topic='world.accepted' AND (o.state='PENDING' OR
                      (o.state='DELIVERING' AND o.lease_expiry<=statement_timestamp()))
                      AND NOT EXISTS (SELECT FROM nf_index_leases l WHERE l.event_id=o.outbox_id
                                      AND l.expires_at>statement_timestamp())
                """ + _INDEX_LIMITS + """
                    ORDER BY o.created_at,o.outbox_id LIMIT 1 FOR UPDATE OF o SKIP LOCKED"""))
                if message_id is None:
                    return None
            lease = self.outbox.claim_in_transaction(repo.conn, worker_id, topic="world.accepted",
                                                      lease_seconds=lease_seconds, message_id=message_id)
            if not lease:
                return None
            if scoped:
                wr = lease["payload"]["world_ref"]
                repo.conn.execute(text("""
                    INSERT INTO nf_index_leases(event_id,token,work_id,branch_id,expires_at)
                    VALUES(:event,:token,:work,:branch,:expiry) ON CONFLICT(event_id)
                    DO UPDATE SET token=EXCLUDED.token,expires_at=EXCLUDED.expires_at
                """), {"event": message_id, "token": token, "work": wr["work_id"], "branch": wr["branch_id"],
                        "expiry": lease["lease_expiry"]})
        try:
            return self.process_claimed(lease, worker_id, fault=fault)
        finally:
            if scoped:
                with self.kernel.db.uow() as repo:
                    repo.conn.execute(text("DELETE FROM nf_index_leases WHERE event_id=:event AND token=:token"),
                                      {"event": message_id, "token": token})

    def dependency_impacts(self, principal, wr, *, limit=1000):
        """Read bounded current owners against the consumed invalidation corpus.

        Scope guards remain the synchronous safety fence while the worker is
        delayed. This report never marks authoritative tasks or facts stale.
        """
        require(1 <= limit <= 10000, "INVALID_ARGUMENT", "Dependency report limit is invalid")
        kernel = self.kernel
        with kernel.db.uow(write=False) as repo:
            grant = kernel.authorize(repo, principal, wr["work_id"], "BRANCH", wr["branch_id"])
            check_profile(grant, "WORLD")
            require(grant["actor_id"] is None, "VIEW_DENIED", "Dependency impacts require a world-control identity")
            revision = kernel.revision(repo, wr)
            require(all(point_key(at) <= point_key(grant["max_story_time"])
                        for at in revision["frontiers"].values()),
                    "VIEW_DENIED", "Dependency report exceeds the grant's story horizon")
            ref_id = str(uuid5(UUID(wr["world_revision_id"]), PROJECTOR_VERSION))
            artifact = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": ref_id})
            if artifact is None:
                return {"world_ref": wr, "status": "INDEX_PENDING", "impacts": [], "complete": False}
            require(artifact["kind"] == "DERIVED_WORLD_INDEX" and artifact["payload"]["world_ref"] == wr
                    and artifact["payload"]["source_state_sha256"] == revision["state_hash"]
                    and fingerprint(artifact["payload"]) == artifact["sha256"],
                    "PROJECTION_CORRUPT", "Derived impact source differs")
            kernel.objects.verify(artifact["sha256"])
            invalidated = {item["scope_key"]: item["version"] for item in artifact["payload"]["invalidations"]}
            dependencies = repo.many("dependencies", {"work_id": wr["work_id"], "branch_id": wr["branch_id"],
                                                       "scope_key": list(invalidated)},
                                     order=["dependency_id"], limit=limit + 1)
            impacts = [{key: row[key] for key in ("owner_type", "owner_id", "scope_key", "dependency_kind")}
                       for row in dependencies[:limit] if row["observed_version"] < invalidated[row["scope_key"]]]
            return {"world_ref": wr, "status": "READY", "artifact_ref": {
                key: artifact[key] for key in ("artifact_id", "artifact_revision_id", "sha256")},
                "impacts": impacts, "complete": len(dependencies) <= limit,
                "authority": "AUTHORITATIVE_SCOPE_GUARDS_REMAIN_REQUIRED"}
