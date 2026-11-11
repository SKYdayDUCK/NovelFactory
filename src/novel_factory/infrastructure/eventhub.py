"""Outbox relay and bounded, transactionally idempotent Redis Streams consumption."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from redis.exceptions import RedisError
from sqlalchemy import text

from novel_factory.infrastructure.event_streams import (
    GROUPS,
    STREAMS,
    SUBSCRIBERS,
    EventEnvelope,
    Streams,
)


def _sql(conn, query, **params):
    return conn.execute(text(query), params)


def _bound(n, maximum=1000):
    if type(n) is not int or not 1 <= n <= maximum:
        raise ValueError("Batch size or time limit exceeds bounds")
    return n


def _str(v):
    return v.decode("ascii") if isinstance(v, bytes) else v


def _stream_id(value):
    return tuple(map(int, _str(value).split("-")))


def _source(conn, event_id):
    row = (
        _sql(
            conn,
            """
        SELECT o.outbox_id,o.topic,o.aggregate_type,o.aggregate_id,o.created_at,
            jsonb_build_object('state',o.payload->'state','work_id',o.payload->'work_id',
                'branch_id',o.payload->'branch_id','task_id',o.payload->'task_id',
                'world_ref',o.payload->'world_ref','generation',o.payload->'generation',
                'trace_id',o.payload->'trace_id','causation_id',o.payload->'causation_id',
                'correlation_id',o.payload->'correlation_id') AS payload,
            t.work_id AS task_work,t.branch_id AS task_branch FROM nf_outbox o
        LEFT JOIN nf_tasks t ON t.task_id=CASE WHEN o.aggregate_type='task'
            AND pg_input_is_valid(o.aggregate_id,'uuid') THEN o.aggregate_id::uuid END
        WHERE o.outbox_id=:id
    """,
            id=event_id,
        )
        .mappings()
        .one_or_none()
    )
    return EventEnvelope.from_source(row) if row else None


class EventHub:
    """Internal service; callbacks may only perform database effects on the supplied connection."""

    def __init__(self, db, redis_client, *, max_failures=5):
        self.db, self.redis = db, redis_client
        self.streams = Streams(redis_client)
        self.max_failures = _bound(max_failures, 100)
        self._rotation = {}
        self._backfill_done = False

    def backfill(self, limit=100):
        with self.db.uow() as repo:
            return _sql(
                repo.conn,
                """
                WITH batch AS MATERIALIZED (
                    SELECT o.outbox_id FROM nf_outbox o WHERE NOT EXISTS (
                        SELECT FROM nf_bus_publications p WHERE p.event_id=o.outbox_id)
                    ORDER BY o.created_at,o.outbox_id LIMIT :n
                ), written AS (
                    INSERT INTO nf_bus_publications(event_id)
                    SELECT outbox_id FROM batch ON CONFLICT DO NOTHING
                ) SELECT count(*) FROM batch
            """,
                n=_bound(limit),
            ).scalar_one()

    def _claim(self, limit, lease_seconds):
        token = str(uuid4())
        with self.db.uow() as repo:
            conn = repo.conn
            ids = (
                _sql(
                    conn,
                    """
                WITH picked AS (
                    SELECT event_id FROM nf_bus_publications WHERE redis_id IS NULL AND retry_at<=now()
                    AND (lease_until IS NULL OR lease_until<=now()) ORDER BY retry_at,event_id
                    LIMIT :n FOR UPDATE SKIP LOCKED
                )
                UPDATE nf_bus_publications p SET attempts=attempts+1,lease_token=:token,
                    lease_until=now()+make_interval(secs=>:ttl) FROM picked
                WHERE p.event_id=picked.event_id RETURNING p.event_id
            """,
                    token=token,
                    ttl=_bound(lease_seconds, 3600),
                    n=_bound(limit),
                )
                .scalars()
                .all()
            )
            events = [_source(conn, ident) for ident in ids]
            for event in events:
                _sql(
                    conn,
                    """
                    UPDATE nf_bus_publications SET stream_key=:key WHERE event_id=:id
                """,
                    key=event.routing_key,
                    id=event.event_id,
                )
                _sql(
                    conn,
                    """
                    INSERT INTO nf_bus_subscriptions(event_id,consumer)
                    SELECT :id,unnest(CAST(:groups AS text[])) ON CONFLICT DO NOTHING
                """,
                    id=event.event_id,
                    groups=list(SUBSCRIBERS[event.routing_key]),
                )
        return token, events

    def relay_once(self, limit=32, lease_seconds=30):
        if not self._backfill_done:
            self._backfill_done = self.backfill(limit) < limit
        token, events = self._claim(limit, lease_seconds)
        if not events:
            return 0
        # XADD runs outside the authoritative transaction. On a partial pipeline
        # failure every ambiguous entry can be sent again under its original UUID.
        try:
            with self.redis.pipeline(transaction=False) as pipe:
                for event in events:
                    pipe.xadd(STREAMS[event.routing_key], {"envelope": event.encode()})
                ids = pipe.execute()
        except RedisError:
            with self.db.uow() as repo:
                _sql(
                    repo.conn,
                    """
                    UPDATE nf_bus_publications SET lease_token=NULL,lease_until=NULL,
                        last_error='REDIS_UNAVAILABLE',retry_at=now()+interval '1 second'
                    WHERE lease_token=:token
                """,
                    token=token,
                )
            raise
        with self.db.uow() as repo:
            repo.conn.execute(
                text("""
                UPDATE nf_bus_publications SET redis_id=:rid,lease_token=NULL,lease_until=NULL,
                    last_error=NULL,checked_at=now() WHERE event_id=:id AND lease_token=:token
            """),
                [
                    {"rid": _str(rid), "id": event.event_id, "token": token}
                    for event, rid in zip(events, ids, strict=True)
                ],
            )
        return len(events)

    def _quarantine(self, conn, stream, consumer, rid, code):
        _sql(
            conn,
            """
            INSERT INTO nf_bus_quarantine(stream_key,consumer,redis_id,error_code)
            VALUES(:stream,:consumer,:rid,:code) ON CONFLICT DO NOTHING
        """,
            stream=stream,
            consumer=consumer,
            rid=rid,
            code=code,
        )

    def _apply(self, stream, group, rid, fields, handler):
        raw = fields.get("envelope", fields.get(b"envelope"))
        try:
            if len(fields) != 1 or not isinstance(raw, (str, bytes)) or len(raw) > 4096:
                raise ValueError
            wire = json.loads(raw)
            event_id = str(UUID(wire["event_id"]))
        except (KeyError, ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
            with self.db.uow() as repo:
                self._quarantine(repo.conn, stream, group, rid, "INVALID_ENVELOPE")
            return True
        with self.db.uow() as repo:
            conn = repo.conn
            delivery = (
                _sql(
                    conn,
                    """
                SELECT status,attempts FROM nf_bus_subscriptions WHERE event_id=:id
                    AND consumer=:group FOR UPDATE
            """,
                    id=event_id,
                    group=group,
                )
                .mappings()
                .one_or_none()
            )
            event = _source(conn, event_id) if delivery else None
            if (
                not event
                or event.routing_key != stream
                or raw not in (event.encode(), event.encode().encode())
            ):
                self._quarantine(conn, stream, group, rid, "SOURCE_MISMATCH")
                return True
            if delivery["status"] != "PENDING":
                return True
            try:
                with conn.begin_nested():
                    inserted = _sql(
                        conn,
                        """
                        INSERT INTO nf_inbox(consumer,message_id) VALUES(:consumer,:id)
                        ON CONFLICT DO NOTHING RETURNING message_id
                    """,
                        consumer="p55:" + group,
                        id=event_id,
                    ).scalar()
                    if inserted:
                        handler(conn, event)
            except Exception:  # noqa: BLE001 -- an isolated processor may raise any application error.
                # SAVEPOINT rolls back both the receipt and any partial SQL effect.
                status = "DEAD" if delivery["attempts"] + 1 >= self.max_failures else "PENDING"
                _sql(
                    conn,
                    """
                    UPDATE nf_bus_subscriptions SET attempts=attempts+1,status=:status,
                        last_error='HANDLER_FAILED',redis_id=:rid WHERE event_id=:id AND consumer=:group
                """,
                    status=status,
                    id=event_id,
                    group=group,
                    rid=rid,
                )
                return status == "DEAD"
            _sql(
                conn,
                """
                UPDATE nf_bus_subscriptions SET status='DONE',attempts=attempts+1,redis_id=:rid,
                    completed_at=clock_timestamp(),last_error=NULL WHERE event_id=:id AND consumer=:group
            """,
                id=event_id,
                group=group,
                rid=rid,
            )
            return True

    def consume_once(self, group, worker_id, handler, *, limit=32, reclaim_idle_ms=30000):
        if group not in GROUPS or not worker_id or len(worker_id) > 128:
            raise ValueError("Invalid consumer identity")
        _bound(limit)
        if not 0 <= reclaim_idle_ms <= 86400000:
            raise ValueError("Invalid claim idle time")
        keys = [key for key, groups in SUBSCRIBERS.items() if group in groups]
        start = self._rotation.get(group, 0)
        self._rotation[group] = (start + 1) % len(keys)
        handled = 0
        for key in keys[start:] + keys[:start]:
            entries = self.streams.messages(key, group, worker_id, limit - handled, reclaim_idle_ms)
            for rid, fields in entries:
                if self._apply(key, group, _str(rid), fields, handler):
                    # Only after the PG transaction has committed.
                    self.redis.xack(STREAMS[key], group, rid)
                handled += 1
            if handled == limit:
                break
        return handled

    def repair_once(self, limit=64):
        """Fairly check retained Redis entries for unfinished subscriptions, including after PITR."""
        with self.db.uow() as repo:
            rows = (
                _sql(
                    repo.conn,
                    """
                WITH picked AS (
                    SELECT p.event_id FROM nf_bus_publications p WHERE p.redis_id IS NOT NULL
                    AND EXISTS(SELECT FROM nf_bus_subscriptions s WHERE s.event_id=p.event_id AND s.status='PENDING')
                    ORDER BY p.checked_at,p.event_id LIMIT :n FOR UPDATE SKIP LOCKED
                )
                UPDATE nf_bus_publications p SET checked_at=clock_timestamp() FROM picked
                WHERE p.event_id=picked.event_id RETURNING p.event_id,p.stream_key,p.redis_id
            """,
                    n=_bound(limit),
                )
                .mappings()
                .all()
            )
        if not rows:
            return 0
        with self.redis.pipeline(transaction=False) as pipe:
            for row in rows:
                pipe.xrange(STREAMS[row["stream_key"]], row["redis_id"], row["redis_id"], count=1)
            results = pipe.execute()
        # After a database PITR an existing stream entry can already be ACKed
        # in a group whose SQL receipt was rolled back. Probe each affected
        # group's cursor and PEL; stream existence alone cannot detect this.
        with self.db.uow(write=False) as repo:
            pending = _sql(
                repo.conn,
                """
                SELECT event_id,consumer FROM nf_bus_subscriptions
                WHERE event_id=ANY(CAST(:ids AS uuid[])) AND status='PENDING'
            """,
                ids=[str(row["event_id"]) for row in rows],
            ).all()
        by_id = {row["event_id"]: row for row in rows}
        keys = {row["stream_key"] for row in rows}
        cursors = {}
        for key in keys:
            if self.redis.exists(STREAMS[key]):
                for info in self.redis.xinfo_groups(STREAMS[key]):
                    group = _str(info["name"])
                    cursors[key, group] = _stream_id(info["last-delivered-id"])
        probes = [
            (by_id[eid], group)
            for eid, group in pending
            if cursors.get((by_id[eid]["stream_key"], group), (0, 0))
            >= _stream_id(by_id[eid]["redis_id"])
        ]
        with self.redis.pipeline(transaction=False) as pipe:
            for row, group in probes:
                pipe.xpending_range(
                    STREAMS[row["stream_key"]], group, row["redis_id"], row["redis_id"], 1
                )
            missing_receipts = {
                r["event_id"]
                for (r, group), pel in zip(probes, pipe.execute(), strict=True)
                if not pel
            }
        restored = 0
        with self.db.uow() as repo:
            for row, entries in zip(rows, results, strict=True):
                event = _source(repo.conn, row["event_id"])
                fields = entries[0][1] if entries else {}
                wire = fields.get("envelope", fields.get(b"envelope"))
                if row["event_id"] in missing_receipts or wire not in (
                    event.encode(),
                    event.encode().encode(),
                ):
                    restored += self._requeue(repo.conn, row["event_id"], row["redis_id"])
        return restored

    @staticmethod
    def _requeue(conn, event_id, rid):
        return _sql(
            conn,
            """
            UPDATE nf_bus_publications SET redis_id=NULL,retry_at=now(),last_error='REPLAY_REQUIRED'
            WHERE event_id=:id AND redis_id IS NOT DISTINCT FROM :rid
        """,
            id=event_id,
            rid=rid,
        ).rowcount

    def redrive(self, event_id, group):
        with self.db.uow() as repo:
            pub = (
                _sql(
                    repo.conn,
                    """
                SELECT redis_id FROM nf_bus_publications WHERE event_id=:id FOR UPDATE
            """,
                    id=str(UUID(event_id)),
                )
                .mappings()
                .one_or_none()
            )
            if not pub:
                return 0
            updated = _sql(
                repo.conn,
                """
                UPDATE nf_bus_subscriptions SET status='PENDING',attempts=0,last_error=NULL
                WHERE event_id=:id AND consumer=:group AND status IN ('DEAD','PENDING')
            """,
                id=event_id,
                group=group,
            ).rowcount
            if updated:
                self._requeue(repo.conn, event_id, pub["redis_id"])
            return updated

    def trim_once(self, retention_seconds=86400, limit=128):
        """Delete a bounded, acknowledged prefix from each stream; retain SQL replay sources."""
        _bound(retention_seconds, 315360000)
        removed = 0
        for key, stream in STREAMS.items():
            entries = self.redis.xrange(stream, count=_bound(limit))
            if not entries:
                continue
            candidates = []
            for rid, fields in entries:
                raw = fields.get("envelope", fields.get(b"envelope"))
                try:
                    if not isinstance(raw, (bytes, str)) or len(raw) > 4096:
                        raise ValueError
                    ident = str(UUID(json.loads(raw)["event_id"]))
                except (TypeError, ValueError, AttributeError, KeyError, RecursionError):
                    ident = None
                candidates.append({"rid": _str(rid), "event_id": ident})
            groups = list(SUBSCRIBERS[key])
            with self.db.uow(write=False) as repo:
                safe = set(
                    _sql(
                        repo.conn,
                        """
                    WITH entries AS (
                        SELECT * FROM jsonb_to_recordset(CAST(:entries AS jsonb)) AS e(rid text,event_id uuid)
                    )
                    SELECT e.rid FROM entries e
                    WHERE NOT EXISTS (SELECT FROM nf_bus_subscriptions s
                        WHERE s.event_id=e.event_id AND s.status<>'DONE')
                    AND (EXISTS (
                        SELECT FROM nf_bus_publications p JOIN nf_outbox o ON o.outbox_id=p.event_id
                        WHERE p.event_id=e.event_id AND p.stream_key=:key AND p.redis_id IS NOT NULL
                        AND p.created_at<now()-make_interval(secs=>:age)
                        AND EXISTS(SELECT FROM nf_bus_subscriptions s WHERE s.event_id=p.event_id)
                    ) OR (SELECT count(*) FROM nf_bus_quarantine q
                        WHERE q.stream_key=:key AND q.redis_id=e.rid AND q.consumer=ANY(CAST(:groups AS text[]))
                        AND q.created_at<now()-make_interval(secs=>:age))=:count)
                """,
                        entries=json.dumps(candidates),
                        key=key,
                        age=retention_seconds,
                        groups=groups,
                        count=len(groups),
                    ).scalars()
                )
            eligible = []
            for item in candidates:
                if item["rid"] not in safe:
                    break
                eligible.append(item["rid"])
            if eligible:
                removed += self.redis.xdel(stream, *eligible)
        return removed

    def status(self):
        with self.db.uow(write=False) as repo:
            groups = (
                _sql(
                    repo.conn,
                    """
                SELECT consumer,status,count(*) AS events,max(completed_at) AS last_success,
                    min(p.created_at) FILTER(WHERE status='PENDING') AS oldest_wait
                FROM nf_bus_subscriptions JOIN nf_bus_publications p USING(event_id) GROUP BY consumer,status
            """,
                )
                .mappings()
                .all()
            )
            relay = (
                _sql(
                    repo.conn,
                    """
                SELECT count(*) FILTER(WHERE redis_id IS NULL) AS backlog,
                    count(*) FILTER(WHERE last_error IS NOT NULL) AS retrying FROM nf_bus_publications
            """,
                )
                .mappings()
                .one()
            )
            quarantine = _sql(repo.conn, "SELECT count(*) FROM nf_bus_quarantine").scalar_one()
        result = {
            "subscriptions": [dict(g) for g in groups],
            "relay": dict(relay),
            "quarantine": quarantine,
        }
        try:
            arrays = {}
            for key, stream in STREAMS.items():
                if self.redis.exists(stream):
                    arrays[key] = self.redis.xinfo_groups(stream)
            result.update(redis="AVAILABLE", streams=arrays)
        except RedisError:
            result.update(redis="UNAVAILABLE")
        return result
