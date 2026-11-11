"""Recover missing observation scopes without modifying accepted receipts."""
from collections import deque
from uuid import uuid5
import json

from alembic import op
from sqlalchemy import text

revision = "0016_p55_observed_scopes"
down_revision = "0015_p55_revalidation"
branch_labels = None
depends_on = None

_ALIASES = {"owner": "legal_owner", "holder": "possessed_by", "active_holder": "possessed_by",
            "last_confirmed_owner": "legal_owner", "location": "located_in"}


def _point(value):
    return value["tick"], value["phase"]


def _active(rel, at):
    interval = rel["valid_time"]
    return (_point(interval["start"]) <= at
            and (interval["end"] is None or at < _point(interval["end"])))


def _missing_read_keys(row):
    details, snapshot = row["details"], row["snapshot"]
    query = details.get("query", {})
    kind = query.get("kind")
    keys, uncertain = set(), set()
    if kind in {"TIMELINE", "NEIGHBORHOOD"}:
        if kind == "TIMELINE":
            nodes = query["subject_ids"]
        else:
            at = _point(details["story_time"]["at"])
            predicates = {_ALIASES.get(p, p) for p in query["predicates"]}
            outgoing = {}
            for rel in snapshot["relations"]:
                if (rel["plane"] == "WORLD" and rel["predicate"] in predicates
                        and rel["object_value"]["kind"] == "ENTITY" and _active(rel, at)):
                    outgoing.setdefault(rel["subject_id"], []).append(rel)
            queue = deque((s, 0) for s in query["seed_ids"])
            seen, nodes, edges = set(query["seed_ids"]), [], set()
            while queue:
                node, depth = queue.popleft()
                nodes.append(node)
                if depth == query["max_hops"]:
                    continue
                for rel in sorted(outgoing.get(node, ()), key=lambda r: r["relation_id"]):
                    if rel["relation_id"] not in edges:
                        if len(edges) >= query["max_edges"]:
                            break
                        edges.add(rel["relation_id"])
                    target = rel["object_value"]["entity_id"]
                    if target not in seen:
                        seen.add(target)
                        queue.append((target, depth + 1))
                if len(edges) >= query["max_edges"] and queue:
                    break
        for node in nodes:
            entity = snapshot["entities"].get(node)
            if entity is not None:
                keys.add("guard:PROCESS_COVERAGE:" + entity.get("scope_id", node))
    else:
        refs = [e["artifact_ref"] for e in details["evidence_refs"] if e.get("kind") == "ARTIFACT"]
        at = _point(details["story_time"]["at"])
        for rel in snapshot["relations"]:
            if rel["plane"] != "WORLD" or rel["predicate"] != "message_content_ref" or not _active(rel, at):
                continue
            try:
                selected = json.loads(rel["object_value"]["value"])
            except (ValueError, TypeError):
                continue
            if selected in refs:
                prefix = "fact:" + rel["subject_id"] + ":"
                keys.add(prefix + "message_content_ref")
                uncertain.update((prefix + "message_authentic", prefix + "attributed_reading_ref"))
    missing = (keys | uncertain) - set(row["keys"])
    return sorted(missing), sorted(missing & uncertain)


def _recover_deliveries(bind):
    # A literary excerpt is itself an authorized, immutable delivery receipt.
    # Recover only objective deliveries of a TEXT actually selected by a carrier.
    rows = bind.execute(text("""
        SELECT a.artifact_revision_id,a.artifact_id,a.sha256,a.created_at,
               a.payload->'text_ref' AS text_ref,i.task_id,i.generation,i.spec->'story_time' AS at,
               x.read_cut->'knowledge_cut' AS cut,v.work_id,v.branch_id,v.world_revision_id,v.branch_seq,v.snapshot
        FROM nf_artifacts a
        JOIN nf_contexts x ON x.context_bundle_id=CAST(a.payload->>'context_bundle_id' AS uuid)
            AND x.task_id=CAST(a.payload->>'task_id' AS uuid)
            AND x.generation=CAST(a.payload->>'generation' AS integer) AND x.work_id=a.work_id
        JOIN nf_task_inputs i ON (i.task_id,i.generation)=(x.task_id,x.generation)
        JOIN nf_revisions v ON v.work_id=a.work_id
            AND v.world_revision_id=CAST(a.payload->'world_ref'->>'world_revision_id' AS uuid)
        WHERE a.kind='TEXT_DELIVERY' AND x.read_cut->'knowledge_cut'->>'plane' IN ('WORLD','SIMULATION')
          AND a.payload->'world_ref'=i.spec->'world_ref'
    """).execution_options(yield_per=16)).mappings()
    try:
        for row in rows:
            delivery = {k: str(row[k]) for k in ("artifact_id","artifact_revision_id","sha256")}
            details = {"origin":"ARTIFACT", "dependency_kind":"SOURCE_OF", "knowledge_cut":row["cut"],
                       "story_time":{"kind":"POINT","at":row["at"]}, "recovered_delivery_ref":delivery,
                       "evidence_refs":[{"kind":"ARTIFACT","artifact_ref":row["text_ref"]}]}
            keys, uncertain = _missing_read_keys({"details": details, "snapshot":row["snapshot"], "keys": []})
            if not keys:
                continue
            # This receipt is additive. Keep the observed time of the actual
            # delivery for downstream material provenance checks.
            obs = uuid5(row["task_id"], "0016:text-delivery:" + str(row["artifact_revision_id"]))
            bind.execute(text("""
                INSERT INTO nf_semantic_reads(observation_id,task_id,generation,work_id,branch_id,world_revision_id,
                                              branch_seq,keys,details,created_at)
                VALUES (:id,:task_id,:generation,:work_id,:branch_id,:world_revision_id,:branch_seq,
                        ARRAY['history','interpretation'],CAST(:details AS jsonb),:created_at)
            """), {**row, "id":obs, "details":json.dumps(details)})
    finally:
        rows.close()


def upgrade():
    op.execute("""
        -- Index corrections are derived from immutable observations, projections
        -- and accepted events. The old receipts remain byte-for-byte available.
        LOCK TABLE nf_semantic_reads,nf_semantic_changes IN SHARE MODE;
        CREATE TABLE nf_semantic_read_supplements (
            observation_id uuid PRIMARY KEY REFERENCES nf_semantic_reads(observation_id),
            branch_id uuid NOT NULL REFERENCES nf_branches(branch_id), branch_seq bigint NOT NULL,
            keys text[] NOT NULL, uncertain_keys text[] NOT NULL
        );
        CREATE INDEX nf_sem_supp_read_keys ON nf_semantic_read_supplements USING gin(keys);
        CREATE TABLE nf_semantic_change_supplements (
            world_revision_id uuid PRIMARY KEY REFERENCES nf_semantic_changes(world_revision_id),
            keys text[] NOT NULL
        );
        CREATE INDEX nf_sem_window_candidate ON nf_conflict_resolutions ((details->'plan_ref'))
            WHERE outcome='REPLAN_PROPOSED';
        INSERT INTO nf_semantic_change_supplements
        SELECT DISTINCT c.world_revision_id, ARRAY['range:possessed_by','range:custody']
        FROM nf_semantic_changes c CROSS JOIN LATERAL jsonb_array_elements(c.details->'evidence_refs') ref
        JOIN nf_events e ON e.event_version_id=CAST(ref->'event_ref'->>'event_version_id' AS uuid)
        CROSS JOIN LATERAL jsonb_array_elements(e.payload->'effects') fx
        WHERE fx->>'kind'='LIFECYCLE_SET' AND fx->>'lifecycle' IN ('DESTROYED','CONSUMED','RETIRED')
          AND NOT c.keys @> ARRAY['range:possessed_by','range:custody'];
    """)
    bind = op.get_bind()
    _recover_deliveries(bind)
    rows = bind.execute(text("""
        SELECT r.observation_id,r.branch_id,r.branch_seq,r.keys,r.details,v.snapshot
        FROM nf_semantic_reads r JOIN nf_revisions v USING(world_revision_id)
        WHERE r.details->'knowledge_cut'->>'plane' IN ('WORLD','SIMULATION')
          AND (r.details->'query'->>'kind' IN ('NEIGHBORHOOD','TIMELINE')
               OR r.details->>'dependency_kind'='SOURCE_OF')
    """).execution_options(yield_per=16)).mappings()
    try:
        for row in rows:
            keys, uncertain = _missing_read_keys(row)
            if keys:
                bind.execute(text("""
                    INSERT INTO nf_semantic_read_supplements VALUES
                      (:id,:branch,:seq,CAST(:keys AS text[]),CAST(:uncertain AS text[]))
                """), {"id": row["observation_id"], "branch": row["branch_id"], "seq": row["branch_seq"],
                       "keys": keys, "uncertain": uncertain})
    finally:
        rows.close()
    op.execute("""
        -- Existing objective consumers may have inherited a producer's
        -- incomplete keys. Carry corrections only along recorded dependencies.
        WITH RECURSIVE lineage AS (
            SELECT observation_id,branch_id,branch_seq,keys,uncertain_keys FROM nf_semantic_read_supplements
            UNION ALL
            SELECT r.observation_id,r.branch_id,r.branch_seq,p.keys,p.uncertain_keys
              FROM lineage p JOIN nf_semantic_reads r ON r.details->>'source_observation_id'=p.observation_id::text
              WHERE r.details->'knowledge_cut'->>'plane' IN ('WORLD','SIMULATION')
        )
        INSERT INTO nf_semantic_read_supplements SELECT * FROM lineage ON CONFLICT DO NOTHING;
        CREATE TRIGGER immutable_semantic_read_supplement BEFORE UPDATE OR DELETE ON nf_semantic_read_supplements
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        CREATE TRIGGER immutable_semantic_change_supplement BEFORE UPDATE OR DELETE ON nf_semantic_change_supplements
            FOR EACH ROW EXECUTE FUNCTION nf_reject_audit_edit();
        -- A prior inbox scan may already have consumed the original hint.
        -- Enqueue only revisions newly intersected by corrected keys.
        INSERT INTO nf_semantic_scans(event_id,work_id,branch_id,world_revision_id)
        SELECT o.outbox_id,c.work_id,c.branch_id,c.world_revision_id
        FROM nf_semantic_changes c JOIN nf_outbox o ON o.dedupe_key='world:'||c.world_revision_id::text
        WHERE EXISTS(SELECT FROM nf_semantic_read_supplements s
                     WHERE s.branch_id=c.branch_id AND s.branch_seq<c.branch_seq AND s.keys && c.keys)
           OR EXISTS(SELECT FROM nf_semantic_change_supplements d JOIN nf_semantic_reads r ON r.keys && d.keys
                     WHERE d.world_revision_id=c.world_revision_id AND r.branch_id=c.branch_id AND r.branch_seq<c.branch_seq)
        ON CONFLICT DO NOTHING;
    """)


def downgrade():
    op.execute("""
        LOCK TABLE nf_semantic_read_supplements,nf_semantic_change_supplements IN ACCESS EXCLUSIVE MODE;
        DO $$ BEGIN
            IF EXISTS (SELECT FROM nf_semantic_read_supplements)
                OR EXISTS (SELECT FROM nf_semantic_change_supplements)
            THEN RAISE EXCEPTION 'Retain or archive semantic index corrections before downgrade'; END IF;
        END $$;
        DROP INDEX nf_sem_window_candidate;
        DROP TABLE nf_semantic_read_supplements,nf_semantic_change_supplements;
    """)
