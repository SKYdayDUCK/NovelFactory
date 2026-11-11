"""Deployment diagnostics for trusted operators. Writes never feed the business Outbox."""

import json
import sys
import time
from datetime import UTC, datetime

from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeout
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as PoolTimeout

_DB = ("DATABASE_UNAVAILABLE", "DATABASE_TIMEOUT", "DATABASE_ERROR")
_REDIS = ("REDIS_UNAVAILABLE", "PUBLISH_FAILED", "REDIS_COMMAND_FAILED")
_REPORT = text("""
    INSERT INTO nf_service_diagnostics(bucket,component,code,first_seen,last_seen,samples)
    VALUES(date_trunc('minute',CAST(:first AS timestamptz)),:component,:code,:first,:last,:samples)
    ON CONFLICT(component,code,bucket) DO UPDATE SET
        first_seen=least(nf_service_diagnostics.first_seen,EXCLUDED.first_seen),
        last_seen=greatest(nf_service_diagnostics.last_seen,EXCLUDED.last_seen),
        samples=nf_service_diagnostics.samples+EXCLUDED.samples,
        recovered_at=CASE WHEN nf_service_diagnostics.recovered_at>=EXCLUDED.last_seen
            THEN nf_service_diagnostics.recovered_at END
""")
_RECOVER = text("""
    UPDATE nf_service_diagnostics SET recovered_at=clock_timestamp()
    WHERE component=:component AND code=ANY(CAST(:codes AS text[])) AND recovered_at IS NULL
""")


def db_error_code(exc):
    if isinstance(exc, PoolTimeout) or getattr(getattr(exc, "orig", None), "sqlstate", None) in {
        "55P03",
        "57014",
    }:
        return "DATABASE_TIMEOUT"
    if isinstance(exc, OperationalError) or getattr(exc, "connection_invalidated", False):
        return "DATABASE_UNAVAILABLE"
    return "DATABASE_ERROR"


class OperationsJournal:
    """One journal per worker thread/relay. Fault notices carry no remote strings or payloads."""

    def __init__(self, db):
        self.db = db
        self.next_report = {}
        self.pending_db = {}

    def _due(self, key):
        now = time.monotonic()
        if now < self.next_report.get(key, 0):
            return False
        self.next_report[key] = now + 30
        return True

    def _db_failure(self, component, exc):
        code = db_error_code(exc)
        now = datetime.now(UTC)
        first, _, samples = self.pending_db.get((component, code), (now, now, 0))
        self.pending_db[component, code] = (first, now, samples + 1)
        if self._due((component, "stderr", code)):
            print(
                json.dumps({"component": component, "code": code, "at": now.isoformat()}),
                file=sys.stderr,
                flush=True,
            )

    def record(self, component, code, count=1):
        if code != "PENDING_RECLAIMED":
            self.next_report.pop((component, "healthy_redis"), None)
            self.next_report.pop((component, "healthy_ping"), None)
            if not self._due((component, code)):
                return
            print(json.dumps({"component": component, "code": code}), file=sys.stderr, flush=True)
        now = datetime.now(UTC)
        try:
            with self.db.uow() as repo:
                repo.conn.execute(
                    _REPORT,
                    {
                        "component": component,
                        "code": code,
                        "first": now,
                        "last": now,
                        "samples": count,
                    },
                )
        except SQLAlchemyError as exc:
            self._db_failure(component, exc)

    def healthy(self, component, redis_confirmed, *, ping_only=False):
        # A periodic heartbeat also closes faults left by a previous process.
        redis_check = redis_confirmed and self._due(
            (component, "healthy_ping" if ping_only else "healthy_redis")
        )
        if not self.pending_db and not redis_check and not self._due((component, "healthy_db")):
            return
        redis_codes = ("REDIS_UNAVAILABLE",) if ping_only else _REDIS
        codes = _DB + (redis_codes if redis_check else ())
        try:
            with self.db.uow() as repo:
                for (source, code), (first, last, count) in self.pending_db.items():
                    repo.conn.execute(
                        _REPORT,
                        {
                            "component": source,
                            "code": code,
                            "first": first,
                            "last": last,
                            "samples": count,
                        },
                    )
                    if source != component:
                        repo.conn.execute(_RECOVER, {"component": source, "codes": list(_DB)})
                repo.conn.execute(_RECOVER, {"component": component, "codes": list(codes)})
            self.pending_db.clear()
            for code in codes:
                self.next_report.pop((component, code), None)
        except SQLAlchemyError as exc:
            self._db_failure(component, exc)

    def run(self, component, operation, *, confirmed_on_empty=False, ping_only=False):
        try:
            count = operation()
        except RedisError as exc:
            if isinstance(exc, (RedisConnectionError, RedisTimeout)):
                self.record(component, "REDIS_UNAVAILABLE")
            else:
                self.record(component, "REDIS_COMMAND_FAILED")
            raise
        except (PoolTimeout, DBAPIError) as exc:
            self._db_failure(component, exc)
            raise
        self.healthy(
            component, redis_confirmed=bool(count or confirmed_on_empty), ping_only=ping_only
        )
        return count


def inspect(db, limit, *, component=None, since=None):
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    params = {"limit": limit, "component": component, "since": since}
    with db.uow(write=False) as repo:
        faults = (
            repo.conn.execute(
                text("""
            SELECT component,code,first_seen,last_seen,samples,recovered_at
            FROM nf_service_diagnostics WHERE (CAST(:component AS text) IS NULL OR component=:component)
            AND (CAST(:since AS timestamptz) IS NULL OR greatest(last_seen,recovered_at)>=:since)
            ORDER BY last_seen DESC,component,code LIMIT :limit
        """),
                params,
            )
            .mappings()
            .all()
        )
        decisions = (
            repo.conn.execute(
                text("""
            SELECT created_at,processor,previous,target,reason,
                sample->'pool_timeouts' AS pool_timeouts,sample->'pool_wait_ms' AS pool_wait_ms
            FROM nf_scale_decisions WHERE reason<>'worker_observation'
            AND (CAST(:component AS text) IS NULL OR processor=:component)
            AND (CAST(:since AS timestamptz) IS NULL OR created_at>=:since)
            ORDER BY id DESC LIMIT :limit
        """),
                params,
            )
            .mappings()
            .all()
        )
        windows = repo.conn.execute(text("""
            SELECT (sample->>'window_end')::timestamptz AS window_end,
                (sample->>'window_start')::timestamptz AS window_start,
                processor, instance, work_id, sample->>'branch_id' AS branch_id,
                sample->>'scope' AS scope, (sample->>'window_seconds')::float8 AS window_seconds,
                (sample->>'busy_slot_seconds')::float8 AS busy_slot_seconds,
                (sample->>'available_slot_seconds')::float8 AS available_slot_seconds,
                (sample->>'worker_utilization_pct')::float8 AS worker_utilization_pct
            FROM nf_scale_decisions WHERE reason='worker_observation'
              AND (CAST(:component AS text) IS NULL OR processor=:component)
              AND (CAST(:since AS timestamptz) IS NULL OR created_at>=:since)
            ORDER BY id DESC LIMIT :limit
        """), params).mappings().all()
    return {
        "observations": [dict(r) for r in faults],
        "scaling_decisions": [dict(r) for r in decisions],
        "worker_utilization_windows": [dict(r) for r in windows],
    }
