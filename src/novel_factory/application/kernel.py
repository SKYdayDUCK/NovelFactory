"""The single application boundary for authoritative world mutations.

P2 scheduling hooks extend the closed command protocol. External actors still
only supply intentions; a sealed, reviewed joint result is accepted here once.
"""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import secrets
from types import SimpleNamespace
from uuid import uuid4, uuid5, UUID

from novel_factory.application.authorization import Principal, ADMIN_CAPABILITIES
from novel_factory.application.semantic_dependencies import check_task_dependencies, record_change, record_proposal
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import tables
from novel_factory.orchestration.tasks import TaskService

def new_id(): return str(uuid4())
def point_key(p): return (p["tick"], p["phase"])
def world_ref(row): return {k:row[k] for k in ("work_id", "branch_id", "world_revision_id")}
def after(p):
    return {"tick": p["tick"], "phase": p["phase"]+1} if p["phase"] < 2147483647 else {"tick": p["tick"]+1, "phase":0}
def require(condition, code, message):
    if not condition: raise DomainError(code, message)


class Kernel:
    def __init__(self, db, objects):
        self.db, self.objects = db, objects
        self._last_verified_projection = None
        self.tasks = TaskService(db.engine, tables, object_store=objects)

    def in_transaction(self, repo):
        """Join an existing write unit of work; nested service calls share its commit."""
        def connection():
            return nullcontext(repo.conn)

        bound = SimpleNamespace(
            engine=SimpleNamespace(begin=connection, connect=connection),
            uow=lambda **kwargs: nullcontext(repo),
        )
        return Kernel(bound, self.objects)

    def authenticate(self, token: str) -> Principal:
        require(isinstance(token,str) and 16 <= len(token) <= 256, "VIEW_DENIED", "Credentials unavailable")
        with self.db.uow(write=False) as r:
            grant = r.one("grants", {"token_hash":sha256(token.encode()).hexdigest()})
            require(grant and grant["active"],
                    "VIEW_DENIED", "Credentials unavailable")
            self._check_resume_grant(r, grant)
            return Principal(grant["grant_id"], grant["principal_id"])

    @staticmethod
    def _check_resume_grant(r, grant):
        if grant["role_id"] == "resume_operator":
            fence = r.one("resume_grants", {"grant_id": grant["grant_id"]}, lock="share")
            work = r.one("works", {"work_id": grant["work_id"]})
            require(fence and fence["expires_at"] > r.now() and fence["policy_version"] == work["policy_version"],
                    "VIEW_DENIED", "Grant expired or policy changed")

    def authorize(self, r, principal, work_id, capability="READ", branch_id=None):
        _, grant = self._authorize_with_work(r, principal, work_id, capability, branch_id)
        return grant

    def _authorize_with_work(self, r, principal, work_id, capability, branch_id):
        """Return both SHARE-locked rows to a caller in this transaction."""
        require(isinstance(principal, Principal), "VIEW_DENIED", "Authenticated identity required")
        work = r.one("works", {"work_id":work_id}, lock="share")
        grant = r.one("grants", {"grant_id":principal.grant_id}, lock="share")
        return self._validate_locked_grant(r, principal, work_id, capability, branch_id, work, grant)

    def _validate_locked_grant(self, r, principal, work_id, capability, branch_id, work, grant):
        """Apply live policy after both authoritative rows have been SHARE-locked."""
        require(work and grant and grant["active"] and grant["principal_id"] == principal.principal_id
                and grant["work_id"] == work_id, "VIEW_DENIED", "Resource unavailable for this identity")
        self._check_resume_grant(r, grant)
        require(capability in grant["capabilities"], "VIEW_DENIED", "Operation is outside this grant")
        require(branch_id is None or grant["branch_ids"] == ["*"] or branch_id in grant["branch_ids"],
                "VIEW_DENIED", "Resource unavailable for this identity")
        return work, grant

    def revision(self, r, wr):
        validate_contract("WorldRef", wr)
        read_text = getattr(r, "revision_with_snapshot_text", None)
        row = read_text(wr) if read_text is not None else r.one("revisions", wr)
        require(row, "VIEW_DENIED", "Requested version is unavailable")
        if read_text is None:
            # Repositories exposing the original decoded-JSON port still prove
            # their snapshot hash independently on each access.
            require(fingerprint(row["snapshot"]) == row["state_hash"],
                    "PROJECTION_CORRUPT", "Projection integrity failed")
        else:
            raw = row["snapshot"]
            # Every read hashes the actual PostgreSQL serialization. Retaining
            # only its digest gives small and large valid worlds the same
            # bounded witness, with no projection bytes or mutable results shared.
            witness = (row["state_hash"], sha256(raw.encode("utf-8")).digest())
            # Decode a private Python object even on a hit. Only evidence that
            # these exact bytes passed NF-CANON-1 is shared between readers.
            row["snapshot"] = json.loads(raw)
            if witness != self._last_verified_projection:
                require(fingerprint(row["snapshot"]) == row["state_hash"],
                        "PROJECTION_CORRUPT", "Projection integrity failed")
                self._last_verified_projection = witness
        from novel_factory.application.simulation import verify_schedule_revision
        verify_schedule_revision(row)
        return row

    def head(self, r, work_id, branch_id=None):
        if branch_id is None:
            work = r.one("works", {"work_id":work_id})
            require(work, "VIEW_DENIED", "Resource unavailable")
            branch_id = work["canon_branch_id"]
        b = r.one("branches", {"work_id":work_id,"branch_id":branch_id})
        require(b, "VIEW_DENIED", "Resource unavailable")
        return {"work_id":work_id,"branch_id":branch_id,"world_revision_id":b["head_revision_id"]}

    def _blob(self, payload):
        content = canonical_bytes(payload) if not isinstance(payload,bytes) else payload
        stored = self.objects.put(content)
        return {"artifact_id":new_id(),"artifact_revision_id":new_id(),"sha256":stored.sha256}

    def _register_artifact(self, r, ref, work_id, payload, kind, principal_id):
        verified = self.objects.verify(ref["sha256"])
        r.add("objects", {"hash":ref["sha256"],"size":verified.size}, ignore_conflict=True)
        r.add("artifacts", {**ref,"work_id":work_id,"kind":kind,"payload":None if isinstance(payload,bytes) else payload,"created_by":principal_id})
        r.add("object_refs", {"owner_type":"artifact","owner_id":ref["artifact_revision_id"],"object_hash":ref["sha256"]})
        if kind.startswith("CREATIVE_"):
            from .harness_observations import artifact_observation
            artifact_observation(r.conn, kind, ref, work_id, payload, principal_id)
        if kind in {"CHANGE_POINT", "REVIEW_REPORT"}:
            from novel_factory.application.audit_log import LogService
            LogService.artifact(r.conn, kind, ref["artifact_revision_id"], work_id, payload, principal_id)

    def artifact(self, r, ref, work_id, *, kind=None):
        validate_contract("ArtifactRef",ref)
        a = r.one("artifacts", {"artifact_revision_id":ref["artifact_revision_id"],"work_id":work_id})
        require(a and all(a[k] == ref[k] for k in ref), "VIEW_DENIED", "Artifact unavailable")
        if kind: require(a["kind"]==kind, "INVALID_ARGUMENT", "Artifact kind mismatch")
        return a

    def _event_sources(self, r, principal, base, event):
        """Validate semantic references against immutable, same-work records.

        A structurally valid UUID/hash is not evidence. This check runs both
        when constructing a proposal and in the final acceptance transaction.
        The caller holds the object-store shared lock across validation and
        commit, so a garbage collector cannot unlink referenced bytes.
        """
        wid=base["work_id"]
        for field,kind in (("ruleset_ref","RULESET"),("adjudication_ref","CLOSURE")):
            ref=event[field]
            self.artifact(r,ref,wid,kind=kind)
            self.objects.verify(ref["sha256"])
        event_time=event.get("occurrence_time",{})
        at=event_time.get("at") if event_time.get("kind")=="EXACT" else None

        def source_event(ref, source_world):
            require(ref["occurrence_id"]!=event["occurrence_id"],"INVALID_EVIDENCE","An occurrence cannot provide its own prior evidence")
            require(source_world["work_id"]==wid,"INVALID_EVIDENCE","Evidence belongs to another work")
            self.authorize(r,principal,wid,"READ",source_world["branch_id"])
            revision=self.revision(r,source_world)
            require(revision["selection"].get(ref["occurrence_id"])==ref["event_version_id"],
                    "INVALID_EVIDENCE","Evidence is not selected at its declared revision")
            require(base["selection"].get(ref["occurrence_id"])==ref["event_version_id"],
                    "INVALID_EVIDENCE","Evidence is outside the proposal's selected history")
            row=r.one("events",{"work_id":wid,"occurrence_id":ref["occurrence_id"],"event_version_id":ref["event_version_id"]})
            require(row,"INVALID_EVIDENCE","Evidence event is unavailable")
            self.objects.verify(row["object_hash"])
            source_time=row["payload"].get("occurrence_time",{})
            require(at is not None and source_time.get("kind")=="EXACT",
                    "INVALID_EVIDENCE","P1 event evidence requires a proven exact temporal order")
            require(point_key(source_time["at"])<=point_key(at),"INVALID_EVIDENCE","Evidence occurs after its claimed acquisition")

        base_ref=world_ref(base)
        for parent in event["causal_parents"]:
            source_event(parent,base_ref)
        for effect in event["effects"]:
            if (effect["kind"] == "RELATION_TRANSITION" and effect["operation"] == "OPEN"
                    and effect["predicate"] in {"message_content_ref", "attributed_reading_ref"}):
                from novel_factory.domain.canonical import strict_json_loads
                ref = strict_json_loads(effect["object_value"]["value"])
                artifact = self.artifact(r, ref, wid)
                require(artifact["kind"] in {"TEXT", "EVIDENCE"}, "INVALID_EVIDENCE",
                        "Message content needs a registered immutable source")
                require(base["snapshot"]["entities"].get(effect["subject_id"], {}).get("entity_kind")
                        in {"KNOWLEDGE", "KNOWLEDGE_CARRIER"}, "INVALID_EVIDENCE", "Message needs a knowledge carrier")
                self.objects.verify(ref["sha256"])
            if effect["kind"]!="COGNITIVE_RECORD_ADD":continue
            require(effect["evidence_refs"],"INVALID_EVIDENCE","Formal cognition requires a source")
            for evidence in effect["evidence_refs"]:
                if evidence["kind"]=="EVENT":
                    source_event(evidence["event_ref"],evidence["world_ref"])
                else:
                    ref=evidence["artifact_ref"]
                    artifact=self.artifact(r,ref,wid)
                    allowed={"RULESET"} if evidence["kind"]=="RULE" else {"TEXT","EVIDENCE","INITIAL_SETUP"}
                    require(artifact["kind"] in allowed,"INVALID_EVIDENCE","Artifact cannot serve as this evidence kind")
                    content=self.objects.read(ref["sha256"])
                    if "span" in evidence:
                        span=evidence["span"]
                        try:decoded=content.decode("utf-8")
                        except UnicodeDecodeError as exc:raise DomainError("INVALID_EVIDENCE","Evidence span is not UTF-8 text") from exc
                        require(0<=span["start"]<span["end"]<=len(decoded),"INVALID_EVIDENCE","Evidence span is outside the source")
                        require(sha256(decoded[span["start"]:span["end"]].encode("utf-8")).hexdigest()==span["quote_sha256"],
                                "INVALID_EVIDENCE","Evidence quotation hash differs from the immutable source")

    def _revalidate_repair(self, r, work_id, genesis, events, *, replacement_ids=()):
        """Replay closed-command prerequisites against each changed prefix.

        A mechanically valid fold does not prove that a later borrower still
        owned the item or that a route remained clear. Arbitrary semantic
        replanning is not implemented: an invalid successor blocks this repair.
        The smaller repair budget bounds the prefix replay work in P1.
        """
        from novel_factory.application.templates import build
        from novel_factory.domain.reducer import replay
        from novel_factory.domain.temporal import validate_temporal_constraints
        require(len(events)<=200,"CAPACITY_LIMIT","P1 reviewed repair is bounded to 200 selected events")
        sources={event["event_version_id"]:p for p in r.many("proposals",{"work_id":work_id}) for event in p["payload"]["event_candidates"]}
        order={oid:i for i,oid in enumerate(validate_temporal_constraints(events))}
        for event in events:
            require(event["occurrence_time"]["kind"]=="EXACT","REPAIR_INCOMPLETE","P1 repair requires resolved command times")
            at=event["occurrence_time"]["at"]
            require(all(e.get("at",at)==at for e in event["effects"]),"REPAIR_INCOMPLETE","P1 repair cannot revalidate delayed effect scheduling")
        ordered=sorted(events,key=lambda e:(point_key(e["occurrence_time"]["at"]),order[e["occurrence_id"]]))
        prefix=[];retained_sources=[]
        selected={e["occurrence_id"]:e["event_version_id"] for e in events}
        for event in ordered:
            source=sources.get(event["event_version_id"])
            if event["event_version_id"] not in replacement_ids:
                require(source,"REPAIR_INCOMPLETE","Repair contains an event without its original command provenance")
                if source["template_id"]!="REPAIR_EVENT":
                    try:actual=build(source["template_id"],source["template_args"],replay(genesis,prefix),source["proposal_id"])
                    except DomainError as exc:
                        raise DomainError("REPAIR_INCOMPLETE","Historical change invalidates a later command prerequisite") from exc
                    require(actual["effects"]==event["effects"],"REPAIR_INCOMPLETE","Historical change invalidates a later command's effects")
            # An unchanged belief is a historical mental record, not a new
            # assertion that its old evidence version is selected now. Preserve
            # exact provenance, expose this impact to review, and never silently
            # rewrite an observer's evidence to the repaired event version.
            for effect in event["effects"]:
                if effect["kind"]!="COGNITIVE_RECORD_ADD":continue
                for evidence in effect["evidence_refs"]:
                    if evidence["kind"]!="EVENT":continue
                    ref=evidence["event_ref"]
                    if selected.get(ref["occurrence_id"])!=ref["event_version_id"]:
                        source_revision=self.revision(r,evidence["world_ref"])
                        require(evidence["world_ref"]["work_id"]==work_id and source_revision["selection"].get(ref["occurrence_id"])==ref["event_version_id"],
                                "REPAIR_INCOMPLETE","Retained cognition has invalid historical provenance")
                        retained_sources.append({"cognition_event_ref":{k:event[k] for k in ("occurrence_id","event_version_id")},
                            "source_world_ref":evidence["world_ref"],"event_ref":ref,"current_selected_version":selected.get(ref["occurrence_id"]),
                            "disposition":"HISTORICAL_PROVENANCE_ONLY","world_truth_recertified":False})
            prefix.append(event)
        return {"selected_events":len(events),"closed_command_prerequisites":"PASS","retained_cognition_sources":retained_sources}

    def put_artifact(self, principal, work_id, payload, kind="TEXT"):
        require(kind in {"TEXT","EVIDENCE","INPUT_MANIFEST","PLAN_MANIFEST"}, "INVALID_ARGUMENT", "This artifact kind is not client writable")
        require(len(payload if isinstance(payload,bytes) else canonical_bytes(payload)) <= 2_000_000,
                "CAPACITY_LIMIT", "Artifact exceeds P1 size budget")
        if kind in {"INPUT_MANIFEST","PLAN_MANIFEST"}:
            validate_contract("InputManifest" if kind=="INPUT_MANIFEST" else "PlanManifest",payload,"kernel")
        with self.db.uow(write=False) as r: self.authorize(r,principal,work_id,"ARTIFACT_WRITE")
        ref = self._blob(payload)
        with self.objects.lock(), self.db.uow() as r:
            self.authorize(r,principal,work_id,"ARTIFACT_WRITE")
            self._register_artifact(r,ref,work_id,payload,kind,principal.principal_id)
        return ref

    def _check_artifact_read(self, r, principal, work_id, ref, *, task_id, generation,
                             context_bundle_id, world_ref, story_time, knowledge_cut):
        """A context is a *task-generation capability*, never a grant-wide allowlist.

        For non-admins the caller must identify the exact active context, its
        immutable story point and knowledge cut. We verify these against the
        database rather than trusting caller-supplied selectors. This is done
        before AND after reading object bytes so a cancelled task or revoked
        grant cannot expose bytes through a read/revocation race.
        """
        grant = self.authorize(r, principal, work_id, "READ")
        artifact = self.artifact(r, ref, work_id)
        if artifact["kind"] == "RUNTIME_ROUTING_POLICY":
            self.authorize(r, principal, work_id, "ADMIN", artifact["payload"]["branch_id"])
            return
        if artifact["kind"].startswith("AGENT_"):
            from .dynamic_runtime import audit_branches

            branches = audit_branches(self, r, work_id, artifact, ref)
            if branches is not None:
                # Some historical inputs were prepared but never assigned to a branch.
                if not branches:
                    require("ADMIN" in grant["capabilities"] and grant["branch_ids"] == ["*"],
                            "VIEW_DENIED", "Agent input has no registered branch")
                    return
                raw = artifact["kind"] != "AGENT_CANDIDATE" or "ADMIN" in grant["capabilities"]
                for branch in branches:
                    self.authorize(r, principal, work_id, "ADMIN" if raw else "READ", branch)
                if raw:
                    return
        if artifact["kind"].startswith("CREATIVE_"):
            # Raw records combine internal prompts, plan choices or evaluation
            # evidence. Task-scoped read ports return their permitted sections.
            payload = artifact["payload"]
            scoped = payload["packet"] if artifact["kind"] == "CREATIVE_CONTEXT_SNAPSHOT" else payload
            world_ref = scoped.get("scope", {}).get("world_ref", {})
            require(world_ref.get("work_id") == work_id and world_ref.get("branch_id"),
                    "VIEW_DENIED", "Creative artifact has no readable branch scope")
            self.authorize(r, principal, work_id, "ADMIN", world_ref["branch_id"])
            return
        if "ADMIN" in grant["capabilities"]:
            return
        from novel_factory.application.queries import WorldQueryService
        from novel_factory.application.query_policy import check_profile
        from novel_factory.domain.temporal import point_key

        require(task_id is not None and generation is not None and context_bundle_id is not None
                and world_ref is not None and story_time is not None and knowledge_cut is not None,
                "VIEW_DENIED", "Current task and knowledge context are required")
        task, current_grant, spec = WorldQueryService(self)._task(r, principal, task_id, generation)
        require(task["work_id"] == work_id and task["context_bundle_id"] == context_bundle_id,
                "VIEW_DENIED", "Context is not bound to the current task generation")
        validate_contract("WorldRef", world_ref)
        validate_contract("StoryPoint", story_time)
        validate_contract("KnowledgeCut", knowledge_cut, "2.1")
        context = r.one("contexts", {"context_bundle_id": context_bundle_id, "work_id": work_id,
                                     "task_id": task_id, "generation": generation})
        require(context is not None and context["grant_id"] == principal.grant_id
                and context["principal_id"] == principal.principal_id,
                "VIEW_DENIED", "Context is unavailable for this task generation")
        read_cut, bundle = context["read_cut"], context["payload"]
        require(world_ref == spec["world_ref"] == read_cut["world_ref"] == bundle["world_ref"]
                and story_time == spec["story_time"] == bundle["story_time"]
                and knowledge_cut == read_cut["knowledge_cut"]
                and bundle["task_id"] == task_id and bundle["generation"] == generation
                and bundle["policy_version"] == read_cut["policy_version"] == spec["policy_version"]
                and read_cut["permission_epoch"] == current_grant["epoch"]
                and bundle["role_id"] == current_grant["role_id"]
                and bundle["actor_id"] == knowledge_cut.get("actor_id")
                and point_key(story_time) <= point_key(current_grant["max_story_time"]),
                "VIEW_DENIED", "Context is no longer valid at this story and authorization cut")
        self.authorize(r, principal, work_id, "READ", world_ref["branch_id"])
        check_profile(current_grant, knowledge_cut["plane"])
        if knowledge_cut["plane"] == "BELIEF":
            require(knowledge_cut["actor_id"] == current_grant["actor_id"]
                    and point_key(knowledge_cut["known_at"]) <= point_key(story_time)
                    and point_key(knowledge_cut["known_at"]) <= point_key(current_grant["max_story_time"]),
                    "VIEW_DENIED", "Evidence belongs to a different actor or knowledge time")
        allowed = [e["artifact_ref"] for e in bundle.get("permitted_evidence_refs", [])
                   if e["kind"] in {"ARTIFACT", "RULE"} and "span" not in e]
        require(ref in allowed, "VIEW_DENIED", "Artifact is outside the current task knowledge cut")

    def get_artifact(self, principal, work_id, ref, *, task_id=None, generation=None,
                     context_bundle_id=None, world_ref=None, story_time=None, knowledge_cut=None):
        """Read immutable evidence; non-admin callers must supply the fixed context fence."""
        context = {"task_id": task_id, "generation": generation,
                   "context_bundle_id": context_bundle_id, "world_ref": world_ref,
                   "story_time": story_time, "knowledge_cut": knowledge_cut}
        with self.db.uow(write=False) as r:
            self._check_artifact_read(r, principal, work_id, ref, **context)
        content = self.objects.read(ref["sha256"])
        scoped = all(value is not None for value in context.values())
        with self.db.uow(write=scoped) as r:
            self._check_artifact_read(r, principal, work_id, ref, **context)
            if scoped:
                from .queries import WorldQueryService
                from .semantic_dependencies import record_material_read
                self.authorize(r, principal, work_id, "READ", world_ref["branch_id"])
                branch = r.one("branches", {"branch_id": world_ref["branch_id"]}, lock="share")
                task, _, spec = WorldQueryService(self)._task(r, principal, task_id, generation, lock="share")
                bundle = r.one("contexts", {"context_bundle_id":context_bundle_id, "task_id":task_id,
                                             "generation":generation})
                require(bundle and task["context_bundle_id"]==context_bundle_id and spec["world_ref"]==world_ref
                        and spec["story_time"]==story_time and bundle["read_cut"]["knowledge_cut"]==knowledge_cut,
                        "VIEW_DENIED", "Artifact read is outside the bound task")
                record_material_read(r, task_id, generation, self.revision(r, world_ref), ref,
                                     knowledge_cut, story_time, branch["head_revision_id"])
        return content

    def bootstrap_work(self, name, genesis, *, frontiers=None, work_id=None):
        """Trusted local author bootstrap; deliberately absent from HTTP/MCP.

        Initializes an isolated work exactly once. Subsequent world state can
        only change via event selection in accept().
        """
        from novel_factory.domain.reducer import replay
        wid=work_id or new_id(); bid=new_id(); rid=new_id(); pid=new_id(); gid=new_id()
        token=secrets.token_urlsafe(32)
        raw=deepcopy(genesis)
        setup={"kind":"INITIAL_SETUP","entities":raw.get("entities",{}),"name":name}
        setup_ref=self._blob(setup)
        for index,rel in enumerate(raw.get("relations",[])):
            rel.setdefault("relation_id",str(uuid5(UUID(wid),f"initial-relation-{index}")))
            rel.setdefault("relation_version_id",str(uuid5(UUID(wid),f"initial-version-{index}")))
            rel.setdefault("plane","WORLD");rel.setdefault("actor_id",None);rel.setdefault("evidence_refs",[])
            rel["origin"]={"kind":"RECORD","record_id":setup_ref["artifact_id"],"record_version_id":setup_ref["artifact_revision_id"],"payload_ref":setup_ref}
        snapshot=replay(raw,[])
        rules={"version":"nf-world-rules-1","closed_world":True,"resource_floor":"0","item_destroy_irreversible":True,
               "templates":["TRANSFER_ITEM","DESTROY_ITEM","RESOURCE_TRANSFER","SET_RELATION","GUARD_LEAVE","PASS_CLEAR_ROUTE"]}
        policy={"version":"nf-policy-1","routine_templates":"AUTHORIZED_CONTROL_PLANE","guarded_approval":"DEPENDENCY_STABLE","retcon":"REPAIR_STRICT_ONLY"}
        interpretation={"reducer":"nf-reducer-1","projector":"nf-projector-1","calendar":"integer-tick-phase-1","predicates":"nf-predicates-1"}
        refs=[self._blob(x) for x in (rules,policy,interpretation)]
        coverage=frontiers or {eid:entity.get("created_at",{"tick":0,"phase":0}) for eid,entity in raw["entities"].items()}
        scopes={"RULESET:world":0,"SCOPE_REGISTRY:world":0}
        for eid in raw["entities"]: scopes["ENTITY:"+eid]=0;scopes["PROCESS_COVERAGE:"+eid]=0
        for p in {x["predicate"] for x in raw.get("relations",[])}: scopes["PREDICATE_RANGE:"+p]=0
        initial={"work_id":wid,"branch_id":bid,"world_revision_id":rid,"branch_seq":0,"parent_revision_id":None,
                 "selection":{},"frontiers":coverage,"scope_versions":scopes,"snapshot":snapshot,"interpretation_ref":refs[2],
                 "manifest_hash":fingerprint({"genesis":fingerprint(raw),"selection":{},"frontiers":coverage}),
                 "state_hash":fingerprint(snapshot),"provenance":{"kind":"INITIALIZATION","setup_ref":setup_ref,"history_epoch":0}}
        with self.objects.lock(),self.db.uow() as r:
            require(not r.one("works",{"work_id":wid}),"ALREADY_EXISTS","Work already initialized")
            r.add("works",{"work_id":wid,"name":name,"policy_version":"nf-policy-1","policy_ref":refs[1],"rules_ref":refs[0],"interpretation_ref":refs[2]})
            for ref,payload,kind in [(setup_ref,setup,"INITIAL_SETUP"),*zip(refs,(rules,policy,interpretation),("RULESET","POLICY","INTERPRETATION"))]:
                self._register_artifact(r,ref,wid,payload,kind,pid)
            r.add("genesis",{"work_id":wid,"payload":raw,"payload_hash":fingerprint(raw)})
            r.add("branches",{"branch_id":bid,"work_id":wid,"kind":"CANON"})
            r.add("revisions",initial)
            for key,version in scopes.items():r.add("scopes",{"work_id":wid,"branch_id":bid,"scope_key":key,"version":version})
            r.change("branches",{"branch_id":bid},{"head_revision_id":rid})
            r.change("works",{"work_id":wid},{"canon_branch_id":bid})
            r.add("grants",{"grant_id":gid,"principal_id":pid,"work_id":wid,"token_hash":sha256(token.encode()).hexdigest(),"role_id":"control_operator","actor_id":None,
                            "branch_ids":["*"],"capabilities":ADMIN_CAPABILITIES,"planes":["WORLD","BELIEF","PLAN","SIMULATION"],"max_story_time":{"tick":9007199254740991,"phase":2147483647}})
            r.outbox("world.initialized","work",wid,{"world_ref":world_ref(initial)},"initial:"+wid)
        return {"world_ref":world_ref(initial),"token":token,"principal":Principal(gid,pid)}

    def issue_grant(self, principal, work_id, *, role_id, actor_id=None, branch_ids=None, capabilities=None, planes=None, max_story_time=None, expires_at=None):
        gid,pid,token=new_id(),new_id(),secrets.token_urlsafe(32)
        with self.db.uow() as r:
            self.authorize(r,principal,work_id,"ADMIN")
            caps=capabilities or ["READ","COMPILE","TASK_CREATE","TASK_CLAIM"]
            pls=planes or ["BELIEF"]
            if role_id == "resume_operator":
                require(isinstance(expires_at, datetime) and expires_at.tzinfo is not None and expires_at > r.now(),
                        "INVALID_ARGUMENT", "Resume grant requires a future expiration")
                require(caps == ["PROJECT_RESUME"] and not actor_id
                        and branch_ids and len(branch_ids) == 1
                        and r.one("branches", {"work_id": work_id, "branch_id": branch_ids[0]}),
                        "INVALID_ARGUMENT", "Resume grants require an expiry and one project branch")
                policy = r.one("works", {"work_id": work_id})["policy_version"]
                pls = []
            else:
                require(expires_at is None and set(caps)<=set(ADMIN_CAPABILITIES),
                        "INVALID_ARGUMENT","Unknown capability or expiry policy")
            if role_id=="character_actor":
                require(actor_id and pls==["BELIEF"] and set(caps)<={"READ","COMPILE","TASK_CREATE","TASK_CLAIM","ARTIFACT_WRITE"},"VIEW_DENIED","Actor grant cannot carry control-plane capabilities")
            r.add("grants",{"grant_id":gid,"principal_id":pid,"work_id":work_id,"token_hash":sha256(token.encode()).hexdigest(),"role_id":role_id,"actor_id":actor_id,
                            "branch_ids":branch_ids or [r.one("works",{"work_id":work_id})["canon_branch_id"]],"capabilities":caps,"planes":pls,
                            "max_story_time":max_story_time or {"tick":0,"phase":0}})
            if role_id == "resume_operator":
                r.add("resume_grants", {"grant_id":gid, "policy_version":policy, "expires_at":expires_at})
        return {"principal":Principal(gid,pid),"token":token}

    def revoke_grant(self, principal, grant_id):
        with self.db.uow() as r:
            target=r.one("grants",{"grant_id":grant_id})
            require(target,"VIEW_DENIED","Grant unavailable")
            self.authorize(r,principal,target["work_id"],"ADMIN")
            require(principal.grant_id!=grant_id,"INVALID_ARGUMENT","Use a distinct administrator to revoke this grant")
            target=r.one("grants",{"grant_id":grant_id},lock="update")
            r.change("grants",{"grant_id":grant_id},{"active":False,"epoch":target["epoch"]+1})
            r.outbox("authorization.revoked","grant",grant_id,{"epoch":target["epoch"]+1},f"revoke:{grant_id}:{target['epoch']+1}")

    def create_task(self, principal, spec, idempotency_key):
        validate_contract("TaskSpec", spec)

        def authorize_task(conn):
            from novel_factory.infrastructure.repository import KernelRepository
            self._authorize_task_create(KernelRepository(conn, tables), principal, spec)

        return self.tasks.create(spec, principal_id=principal.principal_id,
                                 idempotency_key=idempotency_key, authorize_in_tx=authorize_task)

    def create_task_in_transaction(self, r, principal, spec, idempotency_key):
        validate_contract("TaskSpec", spec)
        self._authorize_task_create(r, principal, spec)
        return self.tasks.create_in_transaction(r.conn, spec, principal_id=principal.principal_id,
                                                idempotency_key=idempotency_key)

    def _authorize_task_create(self, r, principal, spec):
        wr = spec["world_ref"]
        g=self.authorize(r,principal,wr["work_id"],"TASK_CREATE",wr["branch_id"])
        self.revision(r,wr)
        from .dynamic_runtime import check_input_branch
        inputs = {ref["artifact_revision_id"]: ref for ref in spec["input_artifacts"]}
        for row in r.many("artifacts", {"work_id": wr["work_id"], "kind": "AGENT_INPUT",
                                        "artifact_revision_id": list(inputs)}):
            check_input_branch(self, r, wr, row, inputs[row["artifact_revision_id"]])
        require(spec["capability_grant_id"]==g["grant_id"] and spec["role_id"]==g["role_id"],"VIEW_DENIED","Task grant mismatch")
        require(point_key(spec["story_time"])<=point_key(g["max_story_time"]),"VIEW_DENIED","Task time outside grant")
        require(spec["context_bundle_id"] is None and spec["dependency_fingerprint"] is None,"INVALID_ARGUMENT","Only compiler may bind task context")
        require(spec["policy_version"]==r.one("works",{"work_id":wr["work_id"]})["policy_version"],"INVALID_ARGUMENT","Task policy version is not current")

    def claim_task(self, principal, worker_id, *, lease_seconds=30, task_id=None):
        with self.db.uow() as repo:
            identity = repo.one("grants", {"grant_id": principal.grant_id})
            require(identity, "VIEW_DENIED", "Grant unavailable")
            grant = self.authorize(repo, principal, identity["work_id"], "TASK_CLAIM")
            branches = grant["branch_ids"]
            return self.tasks.claim_in_transaction(
                repo.conn, worker_id, work_id=grant["work_id"], principal_id=principal.principal_id,
                grant_id=principal.grant_id, branch_ids=None if branches == ["*"] else branches,
                task_id=task_id, lease_seconds=lease_seconds,
            )

    def _task_owner(self,r,principal,task_id,capability="READ"):
        task=r.one("tasks",{"task_id":task_id})
        require(task,"VIEW_DENIED","Task unavailable")
        g=self.authorize(r,principal,task["work_id"],capability,task["branch_id"])
        self._require_task_owner(task, principal)
        return task,g

    @staticmethod
    def _require_task_owner(task, principal):
        require(task["principal_id"] == principal.principal_id and task["grant_id"] == principal.grant_id,
                "VIEW_DENIED", "Task unavailable")

    @staticmethod
    def _public_task(task, world_ref):
        return {**{k: v for k, v in task.items() if k not in {"principal_id", "grant_id", "lease_owner"}},
                "world_ref": world_ref}

    def get_task(self,principal,task_id):
        with self.db.uow(write=False) as r:
            task,_=self._task_owner(r,principal,task_id)
            world_ref = r.one("task_inputs", {"task_id": task_id, "generation": task["generation"]})["spec"]["world_ref"]
        return self._public_task(task, world_ref)

    def get_task_with_locked_work(self, principal, task_id):
        """Read the exact generation and SHARE-lock its owning work before the grant."""
        if not isinstance(principal, Principal):
            # Preserve the regular path's error precedence and locking behavior.
            return Kernel.get_task(self, principal, task_id)
        with self.db.uow(write=False) as repo:
            task, work, world_ref = repo.task_with_locked_work(task_id)
            require(task is not None, "VIEW_DENIED", "Task unavailable")
            grant = repo.one("grants", {"grant_id": principal.grant_id}, lock="share")
            self._validate_locked_grant(repo, principal, task["work_id"], "READ", task["branch_id"], work, grant)
            self._require_task_owner(task, principal)
            require(world_ref is not None, "INTERNAL_ERROR", "Task input unavailable")
            return self._public_task(task, world_ref)

    def cancel_task(self,principal,task_id,generation,idempotency_key):
        def authorize_cancel(conn):
            from novel_factory.infrastructure.repository import KernelRepository
            self._task_owner(KernelRepository(conn,tables),principal,task_id)
        return self.tasks.cancel(task_id,generation,principal_id=principal.principal_id,idempotency_key=idempotency_key,authorize_in_tx=authorize_cancel)

    def submit_result(self,principal,result,worker_id):
        def authorize_result(conn):
            from novel_factory.infrastructure.repository import KernelRepository
            self._task_owner(KernelRepository(conn,tables),principal,result["task_id"])
        return self.tasks.store_result(result,worker_id=worker_id,authorize_in_tx=authorize_result)

    def _prior(self,r,principal,work_id,command_type,key,request):
        prior=r.one("receipts",{"work_id":work_id,"principal_id":principal.principal_id,"command_type":command_type,"idempotency_key":key})
        if prior:
            require(prior["request_hash"]==fingerprint(request),"IDEMPOTENCY_MISMATCH","Idempotency key is already bound to different content")
            return prior["payload"]

    def _receipt(self,r,principal,wr,kind,key,request,payload,proposal_id=None):
        r.add("receipts",{"work_id":wr["work_id"],"principal_id":principal.principal_id,"command_type":kind,"idempotency_key":key,
            "request_hash":fingerprint(request),"branch_id":wr["branch_id"],"proposal_id":proposal_id,"payload":payload})
        if kind in {"PLAN_ADOPT", "CONTINUITY_PUBLISH", "RELEASE_FREEZE"}:
            from novel_factory.application.literary_statistics import record_selection
            record_selection(r.conn, kind, wr, payload, principal.principal_id)

    def _task_binding(self,r,principal,task_id,generation,context_bundle_id=None):
        task=self.tasks.guard_task(r.conn,task_id,generation)
        require(task["principal_id"]==principal.principal_id and task["grant_id"]==principal.grant_id,"VIEW_DENIED","Task unavailable")
        if context_bundle_id is not None:require(task["context_bundle_id"]==context_bundle_id,"STALE_GENERATION","Context does not belong to this task generation")
        context=r.one("contexts",{"context_bundle_id":task["context_bundle_id"]})
        require(context and context["task_id"]==task_id and context["generation"]==generation and context["principal_id"]==principal.principal_id,
                "STALE_GENERATION","Context binding is unavailable")
        require(context["payload"]["world_ref"]==task["world_ref"],"TASK_REBIND_REQUIRED","Task input WorldRef is immutable")
        from novel_factory.application.planning import validate_task_plan_binding
        validate_task_plan_binding(r, self, task)
        return task,context

    def propose(self,principal,task_id,generation,template_id,args,idempotency_key):
        from novel_factory.application.templates import build, GUARDED
        from novel_factory.domain.reducer import replay
        command={"task_id":task_id,"generation":generation,"template_id":template_id,"args":args}
        with self.db.uow(write=False,repeatable_read=True) as r:
            task,g=self._task_owner(r,principal,task_id,"PROPOSE")
            wr=r.one("task_inputs",{"task_id":task_id,"generation":task["generation"]})["spec"]["world_ref"]
            prior=self._prior(r,principal,wr["work_id"],"PROPOSE",idempotency_key,command)
            if prior:return prior
            task,context=self._task_binding(r,principal,task_id,generation)
            wr=task["world_ref"];base=self.revision(r,wr);work=r.one("works",{"work_id":wr["work_id"]})
            require("APPROVE" in g["capabilities"],"VIEW_DENIED","P1 template execution requires an authorized control-plane decision")
            branch=r.one("branches",{"branch_id":wr["branch_id"]})
            if template_id=="REPAIR_EVENT":
                require("REPAIR" in g["capabilities"] and branch["kind"]=="REPAIR","VIEW_DENIED","Historical replacement requires a repair branch")
            genesis=r.one("genesis",{"work_id":wr["work_id"]})["payload"]
            selected_ids=list(base["selection"].values())
            selected_rows=r.many("events",{"work_id":wr["work_id"],"event_version_id":selected_ids}) if selected_ids else []
            selected_by_id={row["event_version_id"]:row["payload"] for row in selected_rows}
            require(all(eid in selected_by_id for eid in selected_ids),"HARD_RULE_VIOLATION","Selected event unavailable")
            # PostgreSQL IN results have no implied order. Retain the exact
            # selected-history order used by the unchanged full reducer.
            selected=[selected_by_id[eid] for eid in selected_ids]
        proposal_id=str(uuid5(UUID(task_id),"proposal:"+idempotency_key))
        footprint=build(template_id,args,base["snapshot"],proposal_id)
        from novel_factory.application.simulation import validate_schedule_command, changeset_extras
        with self.db.uow(write=False, repeatable_read=True) as r:
            self.authorize(r, principal, wr["work_id"], "PROPOSE", wr["branch_id"])
            validate_schedule_command(self, r, principal, base, template_id, args, footprint)
        at=args.get("at",args.get("target"))
        if at:
            require(point_key(at)<=point_key(context["payload"]["story_time"]),"VIEW_DENIED","Command extends beyond its task story cut")
        closure={"kind":"CONTROL_PLANE_CLOSED_COMMAND","template_id":template_id,"template_version":"1",
                 "proposal_id":proposal_id,"args_sha256":fingerprint(args),"base_world_ref":wr,
                 "scope_registry":"p1-closed-world-1","registered_external_channels":[],"process_scopes":footprint["process_scopes"]}
        closure_ref=self._blob(closure)
        event_candidates=[];operations=[]
        if template_id=="REPAIR_EVENT":
            event=deepcopy(args["replacement_event"])
            prior_version=base["selection"].get(args["occurrence_id"])
            require(prior_version and event["occurrence_id"]==args["occurrence_id"],"INVALID_ARGUMENT","Repair occurrence selection mismatch")
            old=next(e for e in selected if e["event_version_id"]==prior_version)
            require(event["event_version_id"]!=old["event_version_id"] and event["effect_group_id"]==old["effect_group_id"] and event["representation_id"]!=old["representation_id"],
                    "HARD_RULE_VIOLATION","Repair must preserve occurrence/effect group and create new event/representation versions")
            operations=[{"operation":"REPLACE","occurrence_id":event["occurrence_id"],"prior_event_version_id":prior_version,"selected_event_version_id":event["event_version_id"]}]
            event_candidates=[event]
            selected=[event if e["event_version_id"]==prior_version else e for e in selected]
        elif footprint["effects"]:
            event={"schema_version":"2.1" if any(e["kind"]=="RELATION_TRANSITION" for e in footprint["effects"]) else "2.0","occurrence_id":str(uuid5(UUID(proposal_id),"occurrence")),"event_version_id":str(uuid5(UUID(proposal_id),"event-v1")),
                   "effect_group_id":str(uuid5(UUID(proposal_id),"effect-group")),"representation_id":str(uuid5(UUID(proposal_id),"representation-v1")),
                   "participants":sorted({key.split(":",1)[1] for key in footprint["reads"] if key.startswith("ENTITY:")}),"causal_parents":[],
                   "ruleset_ref":work["rules_ref"],"adjudication_ref":closure_ref,"effects":footprint["effects"],"engine_version":"nf-reducer-1",
                   "occurrence_time":{"kind":"EXACT","at":at}}
            event_candidates=[event]
            operations=[{"operation":"ADD","occurrence_id":event["occurrence_id"],"prior_event_version_id":None,"selected_event_version_id":event["event_version_id"]}]
            selected.append(event)
        replay(genesis,selected)  # Deterministic validation outside DB transaction.
        advances=[]
        if template_id!="REPAIR_EVENT":
            for scope in footprint["process_scopes"]:
                current=base["frontiers"].get(scope,{"tick":0,"phase":0})
                target=footprint.get("advance_targets",{}).get(scope,
                    args["target"] if template_id in {"ADVANCE_SCOPE","SCHEDULE_ADVANCE"} else after(at))
                require(point_key(target)>point_key(current),"HISTORY_REPAIR_REQUIRED","Command changes already closed history; use repair")
                if at and template_id!="ADVANCE_SCOPE":require(point_key(at)>=point_key(current),"HISTORY_REPAIR_REQUIRED","Event is earlier than scope coverage")
                advances.append({"process_id":scope,"expected_frontier":current,"evaluated_frontier":target,"coverage":"COMPLETE",
                                 "registry_version":"p1-closed-world-1","proof_artifact_ref":closure_ref})
        schema_version="2.1" if any(e["schema_version"]=="2.1" for e in event_candidates) else "2.0"
        payload={"schema_version":schema_version,"proposal_id":proposal_id,"input_world_ref":wr,"event_candidates":event_candidates,"event_operations":operations,
                 "adjudication_refs":[closure_ref],"process_advances":advances,"record_operations":[],"trigger_evaluations":[],
                 "dependency_fingerprint":context["dependency_fingerprint"]}
        payload.update(changeset_extras(base, template_id, args, event_candidates))
        validate_contract("WorldChangeSetR1" if schema_version=="2.1" else "WorldChangeSet",payload,schema_version)
        proposal_ref=self._blob(payload)
        event_blobs=[(self._blob(e),e) for e in event_candidates]
        approval_id=new_id() if template_id!="REPAIR_EVENT" else None
        with self.objects.lock(),self.db.uow() as r:
            self.authorize(r,principal,wr["work_id"],"PROPOSE",wr["branch_id"])
            locked_branch=r.one("branches",{"branch_id":wr["branch_id"]},lock="update")
            prior=self._prior(r,principal,wr["work_id"],"PROPOSE",idempotency_key,command)
            if prior:return prior
            self._task_binding(r,principal,task_id,generation,context["context_bundle_id"])
            validate_schedule_command(self, r, principal, base, template_id, args, footprint)
            self._register_artifact(r,closure_ref,wr["work_id"],closure,"CLOSURE",principal.principal_id)
            for event in event_candidates:self._event_sources(r,principal,base,event)
            if template_id=="REPAIR_EVENT":self._revalidate_repair(r,wr["work_id"],genesis,selected,replacement_ids=[e["event_version_id"] for e in event_candidates])
            self._register_artifact(r,proposal_ref,wr["work_id"],payload,"WORLD_PROPOSAL",principal.principal_id)
            for ref,event in event_blobs:self._register_artifact(r,ref,wr["work_id"],event,"EVENT_CANDIDATE",principal.principal_id)
            r.add("proposals",{"proposal_id":proposal_id,"work_id":wr["work_id"],"branch_id":wr["branch_id"],"principal_id":principal.principal_id,
                "task_id":task_id,"generation":generation,"context_bundle_id":context["context_bundle_id"],"artifact_ref":proposal_ref,"payload":payload,
                "template_id":template_id,"template_args":args,"footprint":footprint,"closure":closure})
            approval={"approval_id":approval_id,"subject_type":"WORLD_PROPOSAL",
                      "subject_sha256":proposal_ref["sha256"],"principal_id":principal.principal_id,"policy_version":work["policy_version"],
                      "dependency_fingerprint":context["dependency_fingerprint"],"disposition":"ACCEPTED","recorded_at":r.now().isoformat(),"conditions":[]}
            if approval_id:
                validate_contract("Approval",approval)
                r.add("approvals",{"approval_id":approval_id,"work_id":wr["work_id"],"proposal_id":proposal_id,"grant_id":principal.grant_id,
                    "payload":approval,"applicability":"DEPENDENCY_STABLE" if template_id in GUARDED else "STRICT_BASE","expires_at":r.now()+timedelta(hours=1)})
            result={"proposal_ref":proposal_ref,"approval_id":approval_id,"base_world_ref":wr,"context_bundle_id":context["context_bundle_id"],
                    "dependency_fingerprint":context["dependency_fingerprint"],"dependency_handle":context["payload"]["dependency_handle"]}
            self._receipt(r,principal,wr,"PROPOSE",idempotency_key,command,result,proposal_id)
            record_proposal(r, base, proposal_id, task_id, generation, footprint, proposal_ref, at,
                            locked_branch["head_revision_id"], template_id)
            for key in sorted(set(footprint["reads"])|set(footprint["writes"])):
                r.add("dependencies",{"dependency_id":new_id(),"work_id":wr["work_id"],"branch_id":wr["branch_id"],"owner_type":"proposal","owner_id":proposal_id,
                    "scope_key":key,"observed_version":base["scope_versions"].get(key,0),"dependency_kind":"HARD_PRECONDITION"})
        return result

    def _proposal(self,r,principal,request):
        wr=request.get("base_world_ref",request.get("expected_world_ref"))
        art=self.artifact(r,request["proposal_ref"],wr["work_id"],kind="WORLD_PROPOSAL")
        p=r.one("proposals",{"proposal_id":art["payload"]["proposal_id"]})
        require(p and p["principal_id"]==principal.principal_id and p["artifact_ref"]==request["proposal_ref"],"VIEW_DENIED","Proposal unavailable")
        require(p["task_id"]==request["task_id"] and p["generation"]==request["generation"] and p["context_bundle_id"]==request["context_bundle_id"]
                and p["payload"]["input_world_ref"]==wr,"INVALID_ARGUMENT","Proposal/task/base binding mismatch")
        return p

    def _approvals(self,r,p,ids,mode):
        require(len(ids)==len(set(ids)),"INVALID_ARGUMENT","Duplicate approval references")
        rows=[]
        for aid in sorted(ids):
            a=r.one("approvals",{"approval_id":aid},lock="share")
            require(a and a["active"] and a["proposal_id"]==p["proposal_id"] and a["expires_at"]>r.now(),"APPROVAL_STALE","Approval qualification is unavailable")
            grant=r.one("grants",{"grant_id":a["grant_id"]},lock="share")
            require(grant and grant["active"] and grant["work_id"]==p["work_id"] and grant["principal_id"]==a["payload"]["principal_id"]
                    and "APPROVE" in grant["capabilities"] and (grant["branch_ids"]==["*"] or p["branch_id"] in grant["branch_ids"]),
                    "APPROVAL_STALE","Approver qualification is unavailable")
            require(a["payload"]["policy_version"]==r.one("works",{"work_id":p["work_id"]})["policy_version"],
                    "APPROVAL_STALE","Approval policy is no longer current")
            require(a["payload"]["subject_sha256"]==p["artifact_ref"]["sha256"] and a["payload"]["dependency_fingerprint"]==p["payload"]["dependency_fingerprint"],
                    "APPROVAL_STALE","Approval content binding mismatch")
            if mode=="DEPENDENCY_GUARDED":require(a["applicability"]=="DEPENDENCY_STABLE","APPROVAL_STALE","This approval requires exact HEAD")
            rows.append(a)
        require(rows,"APPROVAL_STALE","A content-bound approval is required")
        return rows

    def _tokens(self,r,wr,footprint,grant,approvals,history_epoch,work):
        versions=r.scope_map(wr["work_id"],wr["branch_id"])
        def token(key,version,owner="BRANCH"):
            kind=key.split(":",1)[0]
            return {"owner_kind":owner,"work_id":wr["work_id"],"branch_id":wr["branch_id"] if owner=="BRANCH" else None,
                    "scope_kind":kind,"scope_key":key,"version":version,"scope_registry_version":"p1-scopes-1","history_epoch":history_epoch}
        reads=[token(k,versions.get(k,0)) for k in footprint["reads"]]
        writes=[token(k,versions.get(k,0)) for k in footprint["writes"]]
        reads.extend([token("AUTHORIZATION:"+grant["grant_id"],grant["epoch"],"WORK"),
                      token("CANON_POINTER:canon",work["canon_epoch"],"WORK"),token("INTERPRETATION:world",work["interpretation_epoch"],"WORK")])
        for a in approvals:reads.append(token("AUTHORIZATION:approval:"+a["approval_id"],a["epoch"],"WORK"))
        return reads,writes

    def prepare(self,principal,request,*,after_sample=None):
        """Issue a certificate from one consistent sampling transaction.

        after_sample is a local fault-test hook, never a transport parameter.
        Certificate tokens are never refreshed after this hook.
        """
        from novel_factory.application.templates import build,GUARDED
        validate_contract("AcceptancePrepareRequest",request,"2.1")
        wr=request["base_world_ref"];mode=request["concurrency_mode"]
        with self.db.uow(write=False,repeatable_read=True) as r:
            grant=self.authorize(r,principal,wr["work_id"],"ACCEPT",wr["branch_id"])
            prior=self._prior(r,principal,wr["work_id"],"PREPARE",request["idempotency_key"],request)
            if prior:return prior
            p=self._proposal(r,principal,request);approvals=self._approvals(r,p,request["approval_refs"],mode)
            task,context=self._task_binding(r,principal,request["task_id"],request["generation"],request["context_bundle_id"])
            require(request["dependency_handle"]==context["payload"]["dependency_handle"],"INVALID_ARGUMENT","Dependency handle mismatch")
            parent=self.head(r,wr["work_id"],wr["branch_id"]);current=self.revision(r,parent)
            require(r.ancestor(wr,parent),"BRANCH_CHANGED","Input is not an ancestor of the validated revision")
            if mode=="STRICT_HEAD":require(parent==wr,"HEAD_CONFLICT","Strict input HEAD changed")
            else:require(p["template_id"] in GUARDED,"UNPROVEN_FOOTPRINT","Template is not eligible for guarded acceptance")
            actual=build(p["template_id"],p["template_args"],current["snapshot"],p["proposal_id"])
            from novel_factory.application.simulation import validate_schedule_command
            validate_schedule_command(self, r, principal, current, p["template_id"], p["template_args"], actual)
            require(actual==p["footprint"],"DEPENDENCY_CONFLICT","Registered command inputs changed; prepare a new proposal")
            work=r.one("works",{"work_id":wr["work_id"]});branch=r.one("branches",{"branch_id":wr["branch_id"]})
            # Both refer to the same immutable row in this repeatable-read
            # transaction. Reuse the validated value, never a cached HEAD.
            original=current if parent==wr else self.revision(r,wr)
            require(original["provenance"].get("history_epoch",0)==branch["history_epoch"],"HISTORY_CHANGED","Historical repair requires a fresh task input and proposal")
            require(current["interpretation_ref"]==work["interpretation_ref"],"STALE_CERTIFICATE","Current interpretation requires a new reviewed world revision")
            check_task_dependencies(r, task["task_id"], task["generation"])
            reads,writes=self._tokens(r,wr,actual,grant,approvals,branch["history_epoch"],work)
            sample={"validated_world_ref":parent,"state_hash":current["state_hash"],"reads":reads,"writes":writes,"command_sha256":fingerprint(p["template_args"]),"validator":"nf-validator-1"}
            dependency=deepcopy(context["dependency_manifest"])
            require(fingerprint(dependency)==context["dependency_fingerprint"],"HARD_RULE_VIOLATION","Stored input dependency integrity failed")
            issued=r.now();ttl=getattr(self,"certificate_ttl_seconds",300)
        if after_sample:after_sample()
        dependency_ref=self._blob(dependency)
        cert={"schema_version":"2.1","certificate_id":new_id(),"task_id":request["task_id"],"generation":request["generation"],"context_bundle_id":request["context_bundle_id"],
              "concurrency_mode":mode,"proposal_ref":request["proposal_ref"],"base_world_ref":wr,"validated_world_ref":parent,
              "dependency_manifest_ref":dependency_ref,"dependency_fingerprint":context["dependency_fingerprint"],"read_scope_tokens":reads,"write_scope_tokens":writes,
              "command_template_id":p["template_id"],"command_template_version":"1","coverage_status":"COMPLETE_FOR_TEMPLATE" if mode=="DEPENDENCY_GUARDED" else "STRICT_BASE_ONLY",
              "validator_version":"nf-validator-1","schedule_closure_refs":p["payload"]["adjudication_refs"],
              "approval_bindings":[{"approval_id":a["approval_id"],"subject_sha256":p["artifact_ref"]["sha256"],"applicability":a["applicability"],
                  "approval_policy_ref":work["policy_ref"],"dependency_fingerprint":context["dependency_fingerprint"]} for a in approvals],
              "policy_version":work["policy_version"],"history_epoch":branch["history_epoch"],"interpretation_ref":current["interpretation_ref"],
              "issued_at":issued.isoformat(),"expires_at":(issued+timedelta(seconds=ttl)).isoformat()}
        validate_contract("AcceptanceCertificate",cert,"2.1");ref=self._blob(cert)
        with self.objects.lock(),self.db.uow() as r:
            self.authorize(r,principal,wr["work_id"],"ACCEPT",wr["branch_id"])
            r.one("branches",{"branch_id":wr["branch_id"]},lock="update")
            prior=self._prior(r,principal,wr["work_id"],"PREPARE",request["idempotency_key"],request)
            if prior:return prior
            self._register_artifact(r,dependency_ref,wr["work_id"],dependency,"DEPENDENCY_MANIFEST",principal.principal_id)
            self._register_artifact(r,ref,wr["work_id"],cert,"ACCEPTANCE_CERTIFICATE",principal.principal_id)
            r.add("certificates",{"certificate_id":cert["certificate_id"],"work_id":wr["work_id"],"proposal_id":p["proposal_id"],"artifact_ref":ref,"payload":cert,"validation_inputs":sample})
            self._receipt(r,principal,wr,"PREPARE",request["idempotency_key"],request,ref)
        return ref

    def approve(self,principal,proposal_ref,work_id,*,idempotency_key):
        """Explicit review, with separation of proposer/reviewer for retcon."""
        command={"proposal_ref":proposal_ref,"work_id":work_id}
        with self.db.uow() as r:
            self.authorize(r,principal,work_id,"APPROVE")
            art=self.artifact(r,proposal_ref,work_id,kind="WORLD_PROPOSAL")
            p=r.one("proposals",{"proposal_id":art["payload"]["proposal_id"]})
            wr=p["payload"]["input_world_ref"]
            self.authorize(r,principal,work_id,"APPROVE",wr["branch_id"])
            r.one("branches",{"branch_id":wr["branch_id"]},lock="update")
            old=self._prior(r,principal,work_id,"APPROVE",idempotency_key,command)
            if old:return old["approval_id"]
            if p["template_id"]=="REPAIR_EVENT":
                self.authorize(r,principal,work_id,"REPAIR",wr["branch_id"])
                require(principal.principal_id!=p["principal_id"],"VIEW_DENIED","Historical repair requires a distinct reviewer")
            aid=new_id();work=r.one("works",{"work_id":work_id})
            approval={"approval_id":aid,"subject_type":"RETCON" if p["template_id"]=="REPAIR_EVENT" else "WORLD_PROPOSAL","subject_sha256":proposal_ref["sha256"],
                "principal_id":principal.principal_id,"policy_version":work["policy_version"],"dependency_fingerprint":p["payload"]["dependency_fingerprint"],
                "disposition":"ACCEPTED","recorded_at":r.now().isoformat(),"conditions":[]}
            from novel_factory.application.templates import GUARDED
            r.add("approvals",{"approval_id":aid,"work_id":work_id,"proposal_id":p["proposal_id"],"grant_id":principal.grant_id,"payload":approval,
                "applicability":"DEPENDENCY_STABLE" if p["template_id"] in GUARDED else "STRICT_BASE","expires_at":r.now()+timedelta(hours=1)})
            self._receipt(r,principal,wr,"APPROVE",idempotency_key,command,{"approval_id":aid})
        return aid

    def revoke_approval(self,principal,work_id,approval_id):
        with self.db.uow() as r:
            self.authorize(r,principal,work_id,"APPROVE")
            row=r.one("approvals",{"approval_id":approval_id,"work_id":work_id},lock="update")
            require(row,"VIEW_DENIED","Approval unavailable")
            proposal=r.one("proposals",{"proposal_id":row["proposal_id"]})
            self.authorize(r,principal,work_id,"APPROVE",proposal["branch_id"])
            r.change("approvals",{"approval_id":approval_id},{"active":False,"epoch":row["epoch"]+1})
            r.outbox("approval.revoked","approval",approval_id,{"epoch":row["epoch"]+1},f"approval-revoke:{approval_id}:{row['epoch']+1}")

    def _verify_tokens(self,r,wr,cert,footprint,grant,approvals,branch,work):
        require(branch["history_epoch"]==cert["history_epoch"] and work["interpretation_ref"]==cert["interpretation_ref"]
                and work["policy_version"]==cert["policy_version"],"STALE_CERTIFICATE","History or interpretation changed")
        reads,writes=self._tokens(r,wr,footprint,grant,approvals,branch["history_epoch"],work)
        require(reads==cert["read_scope_tokens"] and writes==cert["write_scope_tokens"],"DEPENDENCY_CONFLICT","A guarded dependency changed")

    def accept(self,principal,request,*,fault=None):
        legacy=request.get("schema_version")=="2.0"
        validate_contract("AcceptWorldRequest" if legacy else "GuardedAcceptRequest",request,"2.0" if legacy else "2.1")
        wr=request.get("expected_world_ref",request.get("base_world_ref"))
        with self.db.uow(write=False) as r:
            # Existing successful receipts are historical reads: a later closed
            # task cannot turn a committed operation into a failed retry.
            self.authorize(r,principal,wr["work_id"],"READ",wr["branch_id"])
            old=self._prior(r,principal,wr["work_id"],"ACCEPT",request["idempotency_key"],request)
            if old:return old
            p=self._proposal(r,principal,request)
            require(request["dependency_fingerprint"]==p["payload"]["dependency_fingerprint"],"INVALID_ARGUMENT","Dependency fingerprint mismatch")
            if legacy:require(p["payload"]["schema_version"]=="2.0","SCHEMA_UNSUPPORTED","Legacy strict input only accepts core 2.0 changesets")
        if legacy:
            with self.db.uow(write=False) as r:context=r.one("contexts",{"context_bundle_id":request["context_bundle_id"]})
            prep={"schema_version":"2.1","task_id":request["task_id"],"generation":request["generation"],"context_bundle_id":request["context_bundle_id"],
                  "concurrency_mode":"STRICT_HEAD","base_world_ref":wr,"proposal_ref":request["proposal_ref"],"approval_refs":request["approval_refs"],
                  "dependency_handle":context["payload"]["dependency_handle"],"idempotency_key":"legacy-prepare:"+request["idempotency_key"]}
            cert_ref=self.prepare(principal,prep)
        else:cert_ref=request["acceptance_certificate_ref"]
        with self.db.uow(write=False) as r:
            ca=self.artifact(r,cert_ref,wr["work_id"],kind="ACCEPTANCE_CERTIFICATE")
            cert=ca["payload"]
        # Slow byte I/O precedes the acceptance transaction. The shared file
        # lock below prevents GC between verification and DB reference commit.
        with self.objects.lock():
            for ref in [cert_ref,request["proposal_ref"],*p["payload"]["adjudication_refs"],cert["dependency_manifest_ref"],cert["interpretation_ref"]]:
                self.objects.verify(ref["sha256"])
            for event in p["payload"]["event_candidates"]:self.objects.verify(fingerprint(event))
            return self._accept_transaction(principal,request,p,cert_ref,cert,legacy,fault)

    def _accept_transaction(self,principal,request,p,cert_ref,cert,legacy,fault):
        from novel_factory.application.templates import build,GUARDED
        from novel_factory.domain.reducer import replay
        wr=request.get("expected_world_ref",request.get("base_world_ref"));mode="STRICT_HEAD" if legacy else request["concurrency_mode"]
        with self.db.uow() as r:
            grant=self.authorize(r,principal,wr["work_id"],"ACCEPT",wr["branch_id"])
            # Approval qualification locks precede branch/task locks. Revocation
            # follows the same ordering; nobody upgrades a held shared lock.
            approvals=self._approvals(r,p,request["approval_refs"],mode)
            work=r.one("works",{"work_id":wr["work_id"]})
            branch=r.one("branches",{"branch_id":wr["branch_id"],"work_id":wr["work_id"]},lock="update")
            require(branch,"VIEW_DENIED","Branch unavailable")
            old=self._prior(r,principal,wr["work_id"],"ACCEPT",request["idempotency_key"],request)
            if old:return old
            require(not r.one("receipts",{"branch_id":wr["branch_id"],"proposal_id":p["proposal_id"],"command_type":"ACCEPT"}),"ALREADY_ACCEPTED","Proposal is already accepted on this branch")
            task,context=self._task_binding(r,principal,request["task_id"],request["generation"],request["context_bundle_id"])
            require(task["world_ref"]==wr,"TASK_REBIND_REQUIRED","Task input does not match the original base")
            if branch["kind"]=="CANON":require(work["canon_branch_id"]==wr["branch_id"],"CANON_MOVED","Formal branch pointer changed")
            record=r.one("certificates",{"certificate_id":cert["certificate_id"],"proposal_id":p["proposal_id"],"work_id":wr["work_id"]})
            require(record and record["artifact_ref"]==cert_ref and record["payload"]==cert and fingerprint(cert)==cert_ref["sha256"],"STALE_CERTIFICATE","Certificate has no trusted issuance record")
            for field,value in {"task_id":request["task_id"],"generation":request["generation"],"context_bundle_id":request["context_bundle_id"],"base_world_ref":wr,
                                "proposal_ref":request["proposal_ref"],"concurrency_mode":mode,"dependency_fingerprint":request["dependency_fingerprint"]}.items():
                require(cert[field]==value,"STALE_CERTIFICATE","Certificate binding mismatch")
            require(sorted(a["approval_id"] for a in cert["approval_bindings"])==sorted(request["approval_refs"]),"APPROVAL_STALE","Certificate approval set mismatch")
            require(datetime.fromisoformat(cert["expires_at"])>r.now(),"STALE_CERTIFICATE","Certificate expired")
            parent=self.head(r,wr["work_id"],wr["branch_id"])
            require(r.ancestor(wr,cert["validated_world_ref"]) and r.ancestor(cert["validated_world_ref"],parent),"BRANCH_CHANGED","Certificate ancestry is not preserved")
            if mode=="STRICT_HEAD":require(parent==wr,"HEAD_CONFLICT","Strict input HEAD changed")
            else:require(p["template_id"] in GUARDED and cert["coverage_status"]=="COMPLETE_FOR_TEMPLATE","UNPROVEN_FOOTPRINT","Unregistered guarded template")
            self._verify_tokens(r,wr,cert,p["footprint"],grant,approvals,branch,work)
            check_task_dependencies(r, task["task_id"], task["generation"])
            current=self.revision(r,parent)
            from novel_factory.application.simulation import validate_schedule_command, attach_schedule_state
            validate_schedule_command(self, r, principal, current, p["template_id"], p["template_args"], p["footprint"])
            if p["template_id"]!="REPAIR_EVENT":
                require(build(p["template_id"],p["template_args"],current["snapshot"],p["proposal_id"])==p["footprint"],"DEPENDENCY_CONFLICT","Current command preconditions differ")
            else:
                require(branch["kind"]=="REPAIR" and work["canon_branch_id"]!=branch["branch_id"] and mode=="STRICT_HEAD","VIEW_DENIED","Retcon must remain on an isolated repair branch")
            selection=deepcopy(current["selection"]);frontiers=deepcopy(current["frontiers"])
            candidates={e["event_version_id"]:e for e in p["payload"]["event_candidates"]}
            for event in candidates.values():self._event_sources(r,principal,current,event)
            for op in p["payload"]["event_operations"]:
                oid=op["occurrence_id"]
                if op["operation"]=="ADD":require(oid not in selection,"DUPLICATE_OCCURRENCE","Occurrence already selected, including inherited history")
                elif op["operation"]=="REPLACE":require(selection.get(oid)==op["prior_event_version_id"],"HEAD_CONFLICT","Repair prior selection changed")
                else:raise DomainError("SCHEMA_UNSUPPORTED","P1 does not expose arbitrary event deactivation")
                selection[oid]=op["selected_event_version_id"]
            for advance in p["payload"]["process_advances"]:
                scope=advance["process_id"]
                require(frontiers.get(scope,{"tick":0,"phase":0})==advance["expected_frontier"],"DEPENDENCY_CONFLICT","Process coverage changed")
                frontiers[scope]=advance["evaluated_frontier"]
            require(len(selection)<=2000,"CAPACITY_LIMIT","P1 synchronous replay is bounded to 2000 selected events")
            persisted_ids=[eid for eid in selection.values() if eid not in candidates]
            persisted=r.many("events",{"work_id":wr["work_id"],"event_version_id":persisted_ids}) if persisted_ids else []
            selected_by_id={row["event_version_id"]:row["payload"] for row in persisted}
            selected_by_id.update(candidates)
            require(all(eid in selected_by_id for eid in selection.values()),"HARD_RULE_VIOLATION","Selected event unavailable")
            events=[selected_by_id[eid] for eid in selection.values()]
            genesis=r.one("genesis",{"work_id":wr["work_id"]})["payload"]
            if p["template_id"]=="REPAIR_EVENT":self._revalidate_repair(r,wr["work_id"],genesis,events)
            snapshot=replay(genesis,events)
            # All effect/causal/resource constraints have passed before writing.
            for eid,event in candidates.items():
                previous=r.one("events",{"event_version_id":eid})
                if previous:
                    require(previous["work_id"]==wr["work_id"] and previous["payload_hash"]==fingerprint(event),"HARD_RULE_VIOLATION","Event version identity is immutable")
                else:
                    r.add("events",{"event_version_id":eid,"work_id":wr["work_id"],"occurrence_id":event["occurrence_id"],"effect_group_id":event["effect_group_id"],
                        "representation_id":event["representation_id"],"payload":event,"payload_hash":fingerprint(event),"object_hash":fingerprint(event)})
            if fault:fault("after_events")
            changed=set(p["footprint"]["bumps"])
            if p["template_id"]=="REPAIR_EVENT":changed|=set(current["scope_versions"])
            scopes=r.bump(wr["work_id"],wr["branch_id"],changed)
            rid=new_id();manifest=fingerprint({"genesis":fingerprint(genesis),"selection":selection,"frontiers":frontiers})
            history_epoch=branch["history_epoch"]+(1 if p["template_id"]=="REPAIR_EVENT" else 0)
            rev={"world_revision_id":rid,"work_id":wr["work_id"],"branch_id":wr["branch_id"],"branch_seq":current["branch_seq"]+1,"parent_revision_id":parent["world_revision_id"],
                 "selection":selection,"frontiers":frontiers,"scope_versions":scopes,"snapshot":snapshot,"interpretation_ref":cert["interpretation_ref"],
                 "manifest_hash":manifest,"state_hash":fingerprint(snapshot),"provenance":{"proposal_ref":p["artifact_ref"],"original_base":wr,"certificate_ref":cert_ref,"history_epoch":history_epoch}}
            attach_schedule_state(current, rev, p)
            manifest=rev["manifest_hash"]
            r.add("revisions",rev)
            record_change(r, rev, p)
            r.add_many("revision_events",[{"world_revision_id":rid,"work_id":wr["work_id"],"branch_id":wr["branch_id"],
                "occurrence_id":event["occurrence_id"],"event_version_id":event["event_version_id"],"effect_group_id":event["effect_group_id"]}
                for event in events])
            r.add("commits",{"world_revision_id":rid,"operations":p["payload"]["event_operations"],"request":request,"request_hash":fingerprint(request)})
            for key in sorted(changed):r.add("invalidations",{"invalidation_id":new_id(),"work_id":wr["work_id"],"branch_id":wr["branch_id"],"world_revision_id":rid,"scope_key":key,"version":scopes[key]})
            changed_count=r.change("branches",{"branch_id":wr["branch_id"],"head_revision_id":parent["world_revision_id"]},{"head_revision_id":rid,"history_epoch":history_epoch,"repair_ready":p["template_id"]=="REPAIR_EVENT" or branch["repair_ready"]})
            require(changed_count==1,"HEAD_CONFLICT","HEAD compare-and-swap failed")
            receipt={"command_id":new_id(),"world_ref":world_ref(rev),"branch_seq":rev["branch_seq"],"accepted_event_refs":[{k:e[k] for k in ("occurrence_id","event_version_id")} for e in candidates.values()],
                     "manifest_sha256":manifest,"recorded_at":r.now().isoformat()}
            if not legacy:receipt.update(original_base_world_ref=wr,validation_world_ref=cert["validated_world_ref"],parent_world_ref=parent,acceptance_certificate_ref=cert_ref,proposal_ref=p["artifact_ref"])
            validate_contract("CommitReceipt" if legacy else "GuardedCommitReceipt",receipt,"2.0" if legacy else "2.1")
            self._receipt(r,principal,wr,"ACCEPT",request["idempotency_key"],request,receipt,p["proposal_id"])
            r.outbox("world.accepted","revision",rid,{"world_ref":world_ref(rev),"scopes":sorted(changed)},"world:"+rid)
            self.tasks.accept_in_transaction(r.conn,request["task_id"],request["generation"])
            if fault:fault("before_commit")
        if fault:fault("after_commit")
        return receipt

    def fork(self,principal,base_world_ref,kind,idempotency_key):
        require(kind in {"CANDIDATE","SIMULATION","REPAIR"},"INVALID_ARGUMENT","Unsupported branch kind")
        request={"base_world_ref":base_world_ref,"kind":kind}
        with self.db.uow() as r:
            self.authorize(r,principal,base_world_ref["work_id"],"REPAIR" if kind=="REPAIR" else "BRANCH",base_world_ref["branch_id"])
            r.one("branches",{"branch_id":base_world_ref["branch_id"]},lock="update")
            old=self._prior(r,principal,base_world_ref["work_id"],"FORK",idempotency_key,request)
            if old:return old
            base=self.revision(r,base_world_ref);bid,rid=new_id(),new_id()
            inherited_epoch=base["provenance"].get("history_epoch",0)
            r.add("branches",{"branch_id":bid,"work_id":base["work_id"],"kind":kind,"fork_ref":base_world_ref,"history_epoch":inherited_epoch})
            fields={k:deepcopy(base[k]) for k in ("work_id","selection","frontiers","scope_versions","snapshot","interpretation_ref","manifest_hash","state_hash")}
            fields.update(branch_id=bid,world_revision_id=rid,branch_seq=0,parent_revision_id=None,provenance={"fork_ref":base_world_ref,"history_epoch":inherited_epoch})
            from novel_factory.application.simulation import inherit_schedule_state
            inherit_schedule_state(base, fields)
            r.add("revisions",fields)
            for key,val in base["scope_versions"].items():r.add("scopes",{"work_id":base["work_id"],"branch_id":bid,"scope_key":key,"version":val})
            for event in r.many("revision_events",{"world_revision_id":base["world_revision_id"]}):
                event.update(world_revision_id=rid,branch_id=bid);r.add("revision_events",event)
            r.change("branches",{"branch_id":bid},{"head_revision_id":rid})
            result=world_ref(fields)
            self._receipt(r,principal,base_world_ref,"FORK",idempotency_key,request,result)
            r.outbox("branch.created","branch",bid,{"world_ref":result,"base_world_ref":base_world_ref},"fork:"+bid)
        return result

    def compare(self,principal,left,right):
        require(left["work_id"]==right["work_id"],"VIEW_DENIED","Cross-work comparison is unavailable")
        with self.db.uow(write=False,repeatable_read=True) as r:
            for wr in (left,right):self.authorize(r,principal,wr["work_id"],"BRANCH",wr["branch_id"])
            a,b=self.revision(r,left),self.revision(r,right)
            keys=set(a["selection"])|set(b["selection"])
            changes=[{"occurrence_id":k,"left_event_version":a["selection"].get(k),"right_event_version":b["selection"].get(k)} for k in sorted(keys) if a["selection"].get(k)!=b["selection"].get(k)]
            return {"left":left,"right":right,"event_changes":changes,"left_state_hash":a["state_hash"],"right_state_hash":b["state_hash"],"automatic_semantic_merge":False}

    def promote(self,principal,old_world_ref,candidate_world_ref,idempotency_key):
        from novel_factory.domain.reducer import replay
        require(old_world_ref["work_id"]==candidate_world_ref["work_id"],"VIEW_DENIED","Cross-work promotion is unavailable")
        wid=old_world_ref["work_id"];request={"old_world_ref":old_world_ref,"candidate_world_ref":candidate_world_ref}
        # Deterministic reconstruction and concrete comparison precede the CAS.
        comparison=self.compare(principal,old_world_ref,candidate_world_ref)
        with self.db.uow(write=False) as r:
            self.authorize(r,principal,wid,"PROMOTE",candidate_world_ref["branch_id"])
            candidate=self.revision(r,candidate_world_ref)
            events=[r.one("events",{"event_version_id":eid,"work_id":wid})["payload"] for eid in candidate["selection"].values()]
            genesis=r.one("genesis",{"work_id":wid})["payload"]
            revalidation=self._revalidate_repair(r,wid,genesis,events)
            candidate_revisions=r.many("revisions",{"work_id":wid,"branch_id":candidate_world_ref["branch_id"]})
            repair_reviews=[]
            for rev in candidate_revisions:
                commit=r.one("commits",{"world_revision_id":rev["world_revision_id"]})
                if commit:
                    proposal_ref=rev["provenance"]["proposal_ref"]
                    art=self.artifact(r,proposal_ref,wid,kind="WORLD_PROPOSAL")
                    proposal=r.one("proposals",{"proposal_id":art["payload"]["proposal_id"]})
                    if proposal["template_id"]=="REPAIR_EVENT":repair_reviews.append((proposal,commit["request"]["approval_refs"]))
        require(fingerprint(replay(genesis,events))==candidate["state_hash"],"REPAIR_INCOMPLETE","Repair reconstruction does not match its projection")
        report={"kind":"P1_REPAIR_REVALIDATION","old_world_ref":old_world_ref,"candidate_world_ref":candidate_world_ref,"comparison":comparison,
                "deterministic_replay":"PASS","prerequisite_revalidation":revalidation,"state_hash":candidate["state_hash"],"literary_review":"NOT_IN_P1","protected_releases":[]}
        report_ref=self._blob(report)
        with self.objects.lock(),self.db.uow() as r:
            # Exclusive work pointer lock first, then grants, sorted branches.
            work=r.one("works",{"work_id":wid},lock="update")
            self.authorize(r,principal,wid,"PROMOTE",candidate_world_ref["branch_id"])
            old=self._prior(r,principal,wid,"PROMOTE",idempotency_key,request)
            if old:return old
            require(repair_reviews,"REPAIR_INCOMPLETE","Repair has no independently reviewed change")
            for proposal,ids in repair_reviews:self._approvals(r,proposal,ids,"STRICT_HEAD")
            for bid in sorted({old_world_ref["branch_id"],candidate_world_ref["branch_id"]}):r.one("branches",{"branch_id":bid},lock="update")
            require(work["canon_branch_id"]==old_world_ref["branch_id"] and self.head(r,wid,old_world_ref["branch_id"])==old_world_ref,"HEAD_CONFLICT","Canon changed during repair")
            b=r.one("branches",{"branch_id":candidate_world_ref["branch_id"]})
            require(b["kind"]=="REPAIR" and b["repair_ready"] and self.head(r,wid,b["branch_id"])==candidate_world_ref,"REPAIR_INCOMPLETE","Candidate head or repair qualification changed")
            require(b["fork_ref"]==old_world_ref,"HEAD_CONFLICT","Repair was prepared against another canon baseline")
            self._register_artifact(r,report_ref,wid,report,"REVALIDATION_REPORT",principal.principal_id)
            r.change("works",{"work_id":wid,"canon_branch_id":old_world_ref["branch_id"],"canon_epoch":work["canon_epoch"]},{"canon_branch_id":b["branch_id"],"canon_epoch":work["canon_epoch"]+1})
            promotion_id=new_id()
            r.add("promotions",{"promotion_id":promotion_id,"work_id":wid,"old_world_ref":old_world_ref,"new_world_ref":candidate_world_ref,"report_ref":report_ref})
            self._receipt(r,principal,candidate_world_ref,"PROMOTE",idempotency_key,request,candidate_world_ref)
            r.outbox("canon.promoted","work",wid,{"old_world_ref":old_world_ref,"world_ref":candidate_world_ref,"epoch":work["canon_epoch"]+1},"promote:"+promotion_id)
        return candidate_world_ref

    def impact(self,principal,wr,scope_keys):
        with self.db.uow(write=False) as r:
            self.authorize(r,principal,wr["work_id"],"BRANCH",wr["branch_id"])
            rev=self.revision(r,wr)
            deps=r.many("dependencies",{"work_id":wr["work_id"],"branch_id":wr["branch_id"],"scope_key":scope_keys})
            return [{"owner_type":d["owner_type"],"owner_id":d["owner_id"],"dependency_kind":d["dependency_kind"],"scope_key":d["scope_key"],
                "stale":d["observed_version"]!=rev["scope_versions"].get(d["scope_key"],0)} for d in deps]

    def collect_objects(self,*,grace_seconds=3600):
        def references():
            with self.db.uow(write=False) as r:return [row["object_hash"] for row in r.many("object_refs")]
        return self.objects.collect(references,grace_seconds=grace_seconds)
