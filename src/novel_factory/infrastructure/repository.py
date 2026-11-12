"""Internal PostgreSQL repository. Never exposed through a transport adapter."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Text, bindparam, select, update, delete, and_, func, text
from sqlalchemy.dialects.postgresql import insert

@lru_cache(maxsize=4)
def _revision_text_query(revision):
    # A live PostgreSQL serialization is an exact content witness. A stored
    # digest or a revision identifier on its own cannot show storage damage.
    columns = [
        col.cast(Text).label("snapshot") if col.key == "snapshot" else col
        for col in revision.c
    ]
    return select(*columns).where(
        revision.c.work_id == bindparam("work_id"),
        revision.c.branch_id == bindparam("branch_id"),
        revision.c.world_revision_id == bindparam("world_revision_id"),
    )


@lru_cache(maxsize=4)
def _tasks_with_world_ref_query(tasks, inputs):
    key = and_(inputs.c.task_id == tasks.c.task_id, inputs.c.generation == tasks.c.generation)
    return (select(tasks, inputs.c.spec["world_ref"].label("world_ref"))
            .select_from(tasks.outerjoin(inputs, key))
            .where(tasks.c.task_id.in_(bindparam("task_ids", expanding=True))))


class KernelRepository:
    def __init__(self, conn, tables):
        self.conn, self.tables = conn, tables

    def one(self, name, where, *, lock=None):
        rows = self.many(name, where, lock=lock, limit=1)
        return rows[0] if rows else None

    def tasks_with_world_ref(self, task_ids):
        """Pin each current row to its immutable input for that exact generation."""
        query = _tasks_with_world_ref_query(self.tables["tasks"], self.tables["task_inputs"])
        return [dict(row) for row in self.conn.execute(query, {"task_ids": task_ids}).mappings()]

    def revision_with_snapshot_text(self, world_ref):
        query = _revision_text_query(self.tables["revisions"])
        row = self.conn.execute(query, world_ref).mappings().one_or_none()
        return dict(row) if row is not None else None

    def many(self, name, where=None, *, lock=None, order=None, limit=None):
        t = self.tables[name]
        q = select(t)
        for key, value in (where or {}).items():
            q = q.where(t.c[key].in_(value) if isinstance(value, (list, set, tuple)) else t.c[key] == value)
        if order:
            q = q.order_by(*(t.c[k] for k in order))
        if lock:
            q = q.with_for_update(read=lock == "share")
        if limit is not None:
            q = q.limit(limit)
        return [dict(r) for r in self.conn.execute(q).mappings()]

    def add(self, name, values, *, ignore_conflict=False):
        q = insert(self.tables[name]).values(**values)
        if ignore_conflict:
            q = q.on_conflict_do_nothing()
        self.conn.execute(q)

    def add_many(self, name, values):
        """Insert bounded batches inside the caller's existing transaction.

        No conflict is ignored and no commit is introduced. A later failure
        rolls every batch back with the surrounding context/receipt changes.
        """
        values = list(values)
        for offset in range(0, len(values), 256):
            self.conn.execute(insert(self.tables[name]).values(values[offset:offset + 256]))

    def change(self, name, where, values):
        t = self.tables[name]
        q = update(t).where(*(t.c[k] == v for k, v in where.items())).values(**values)
        return self.conn.execute(q).rowcount

    def bump(self, work_id, branch_id, keys):
        unique_keys = list(set(keys))
        if unique_keys:
            # Order conflicting rows as the former sorted loop did. COLLATE "C"
            # compares UTF-8 bytes, which preserves Unicode code point order.
            self.conn.execute(text("""
                INSERT INTO nf_scopes (work_id, branch_id, scope_key, version)
                SELECT :work_id, :branch_id, key, 1
                FROM unnest(CAST(:keys AS text[])) AS requested(key)
                ORDER BY key COLLATE "C"
                ON CONFLICT (work_id, branch_id, scope_key)
                DO UPDATE SET version = nf_scopes.version + 1
            """), {"work_id": work_id, "branch_id": branch_id, "keys": unique_keys})
        return self.scope_map(work_id, branch_id)

    def scope_map(self, work_id, branch_id):
        return {r["scope_key"]:r["version"] for r in self.many("scopes", {"work_id":work_id,"branch_id":branch_id}, order=["scope_key"])}

    def ancestor(self, older, newer):
        if any(older[k] != newer[k] for k in ("work_id", "branch_id")):
            return False
        # Immutable single-parent chain plus composite FKs; verify reachability,
        # never infer cross-branch ancestry from integer sequence alone. Only
        # identity/parent metadata is needed: loading two full JSONB world
        # projections here adds decoding and allocation to every guarded write.
        # Scope BOTH the starting row and every recursive edge, so a forged
        # work/branch around a real revision cannot prove ancestry.
        return bool(self.conn.execute(text("""
          WITH RECURSIVE chain AS (
            SELECT world_revision_id,parent_revision_id FROM nf_revisions
            WHERE world_revision_id=:new AND work_id=:work AND branch_id=:branch
            UNION ALL
            SELECT r.world_revision_id,r.parent_revision_id FROM nf_revisions r
            JOIN chain c ON r.world_revision_id=c.parent_revision_id
            WHERE r.work_id=:work AND r.branch_id=:branch
          ) SELECT EXISTS(SELECT 1 FROM chain WHERE world_revision_id=:old)
        """), {"new":newer["world_revision_id"],"old":older["world_revision_id"],
                 "work": newer["work_id"], "branch": newer["branch_id"]}).scalar_one())

    def now(self):
        return self.conn.execute(select(func.clock_timestamp())).scalar_one()

    def outbox(self, topic, aggregate_type, aggregate_id, payload, dedupe_key):
        from novel_factory.orchestration.tasks import enqueue_outbox
        return enqueue_outbox(self.conn, self.tables, topic, aggregate_type, aggregate_id, payload, dedupe_key)
