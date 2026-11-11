import json
from datetime import UTC, datetime
from uuid import uuid4

from novel_factory.infrastructure.event_streams import SUBSCRIBERS, EventEnvelope


def test_envelope_projects_only_whitelisted_metadata():
    ident = str(uuid4())
    base = {
        "outbox_id": ident,
        "created_at": datetime.now(UTC),
        "aggregate_type": "task",
        "aggregate_id": ident,
        "topic": "task.changed",
        "payload": {
            "state": "READY",
            "generation": 0,
            "prompt": "sensitive text",
            "trace_id": "sensitive text",
            "world_ref": {"work_id": "sensitive text"},
        },
    }
    event = EventEnvelope.from_source(base)
    data = json.loads(event.encode())
    assert data["event_id"] == data["task_id"] == data["trace_id"] == ident
    assert data["world_ref"] is None and data["generation"] == 0
    assert data["routing_key"] == "model" and "model" in SUBSCRIBERS["model"]
    assert "sensitive" not in event.encode()
    assert "sensitive" not in EventEnvelope.from_source({**base, "topic": "sensitive"}).encode()


def test_audit_goes_only_to_stats_and_cannot_recursively_create_logs():
    assert SUBSCRIBERS["observability"] == ("statistics",)


def test_wire_schema_matches_runtime_and_rejects_extra_payload():
    from pathlib import Path

    import jsonschema
    import pytest

    from novel_factory.infrastructure.event_streams import STREAMS, TOPICS

    schema = json.loads(
        (
            Path(__file__).resolve().parents[2] / "contracts/transport/event-envelope-v1.json"
        ).read_text()
    )
    assert set(schema["properties"]["event_type"]["enum"]) == TOPICS | {"domain.changed"}
    assert set(schema["properties"]["routing_key"]["enum"]) == set(STREAMS)
    ident = str(uuid4())
    event = EventEnvelope.from_source(
        {
            "outbox_id": ident,
            "created_at": datetime.now(UTC),
            "aggregate_type": "audit",
            "aggregate_id": ident,
            "topic": "audit.recorded",
            "payload": {},
        }
    )
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    data = json.loads(event.encode())
    validator.validate(data)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate({**data, "prompt": "secret"})
