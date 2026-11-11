from datetime import datetime, timezone
import json
from uuid import uuid4

from novel_factory.infrastructure.event_streams import EventEnvelope, SUBSCRIBERS


def test_envelope_projects_only_whitelisted_metadata():
    ident = str(uuid4())
    base = dict(outbox_id=ident, created_at=datetime.now(timezone.utc), aggregate_type="task",
                aggregate_id=ident, topic="task.changed", payload={
                    "state": "READY", "generation": 0, "prompt": "sensitive text",
                    "trace_id": "sensitive text", "world_ref": {"work_id": "sensitive text"}})
    event = EventEnvelope.from_source(base)
    data = json.loads(event.encode())
    assert data["event_id"] == data["task_id"] == data["trace_id"] == ident
    assert data["world_ref"] is None and data["generation"] == 0
    assert data["routing_key"] == "model" and "model" in SUBSCRIBERS["model"]
    assert "sensitive" not in event.encode()
    assert "sensitive" not in EventEnvelope.from_source({**base, "topic": "sensitive"}).encode()


def test_audit_goes_only_to_stats_and_cannot_recursively_create_logs():
    assert SUBSCRIBERS["observability"] == ("statistics",)
