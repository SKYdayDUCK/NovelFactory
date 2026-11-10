"""Internal PostgreSQL repository. Never exposed through a transport adapter."""
from __future__ import annotations

from sqlalchemy import select, update, delete, and_, func, text
from sqlalchemy.dialects.postgresql import insert

class KernelRepository:
    def __init__(self, conn, tables):
        self.conn, self.tables = conn, tables

    def one(self, name, where, *, lock=None):
        rows = self.many(name, where, lock=lock, limit=1)
        return rows[0] if rows else None

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

    def change(self, name, where, values):
        t = self.tables[name]
        q = update(t).where(*(t.c[k] == v for k, v in where.items())).values(**values)
        return self.conn.execute(q).rowcount

    def bump(self, work_id, branch_id, keys):
        t = self.tables["scopes"]
        for key in sorted(set(keys)):
            q = insert(t).values(work_id=work_id, branch_id=branch_id, scope_key=key, version=1)
            self.conn.execute(q.on_conflict_do_update(index_elements=[t.c.work_id,t.c.branch_id,t.c.scope_key],
                set_={"version": t.c.version + 1}))
        return self.scope_map(work_id, branch_id)

    def scope_map(self, work_id, branch_id):
        return {r["scope_key"]:r["version"] for r in self.many("scopes", {"work_id":work_id,"branch_id":branch_id}, order=["scope_key"])}

    def ancestor(self, older, newer):
        if any(older[k] != newer[k] for k in ("work_id", "branch_id")):
            return False
        a = self.one("revisions", older)
        b = self.one("revisions", newer)
        if not a or not b or a["branch_seq"] > b["branch_seq"]:
            return False
        # Immutable single-parent chain plus composite FKs; verify reachability,
        # never infer cross-branch ancestry from integer sequence alone.
        return bool(self.conn.execute(text("""
          WITH RECURSIVE chain AS (
            SELECT world_revision_id,parent_revision_id FROM nf_revisions WHERE world_revision_id=:new
            UNION ALL
            SELECT r.world_revision_id,r.parent_revision_id FROM nf_revisions r
            JOIN chain c ON r.world_revision_id=c.parent_revision_id
          ) SELECT EXISTS(SELECT 1 FROM chain WHERE world_revision_id=:old)
        """), {"new":newer["world_revision_id"],"old":older["world_revision_id"]}).scalar_one())

    def now(self):
        return self.conn.execute(select(func.clock_timestamp())).scalar_one()

    def outbox(self, topic, aggregate_type, aggregate_id, payload, dedupe_key):
        from novel_factory.orchestration.tasks import enqueue_outbox
        return enqueue_outbox(self.conn, self.tables, topic, aggregate_type, aggregate_id, payload, dedupe_key)
