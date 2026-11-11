"""Version 1 wire contract. Stream traffic contains only metadata and references."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC
from uuid import UUID

from redis.exceptions import ResponseError

DEFAULT_HANDLER_FAILURE_LIMIT = 5

STREAMS = {
    "domain": "nf:events:domain:v1",
    "model": "nf:jobs:model:v1",
    "world": "nf:jobs:world:v1",
    "compute": "nf:jobs:compute:v1",
    "observability": "nf:events:observability:v1",
    "maintenance": "nf:jobs:maintenance:v1",
}
SUBSCRIBERS = {
    "domain": ("world", "compute", "recovery", "log"),
    "model": ("model", "world", "recovery", "log"),
    "world": ("world", "log"),
    "compute": ("compute", "log"),
    "maintenance": ("recovery", "log"),
    "observability": ("statistics",),
}
GROUPS = {group for groups in SUBSCRIBERS.values() for group in groups}
TOPICS = frozenset(
    [
        "world.accepted",
        "world.initialized",
        "authorization.revoked",
        "approval.revoked",
        "branch.created",
        "canon.promoted",
        "continuity.published",
        "planning.candidate",
        "planning.world_reviewed",
        "planning.selected",
        "planning.replanned",
        "planning.window_proposed",
        "narrative.draft",
        "narrative.accepted",
        "refinement.proved",
        "release.frozen",
        "context.compiled",
        "volume.successor.registered",
        "volume.successor.changed",
        "volume.settlement.recorded",
        "volume.settlement.continuity",
        "volume.settlement.publication",
        "supervisor.handoff.blocked",
        "supervisor.task.expired",
        "supervisor.adjudication.delivery",
        "supervisor.joint.delivery",
        "supervisor.editorial.delivery",
        "supervisor.planning.delivery",
        "supervisor.narrative.delivery",
        "supervisor.enrolled",
        "workflow.wait.closed",
        "task.changed",
        "audit.recorded",
        "world.requested",
        "compute.requested",
        "recovery.requested",
    ]
)


def route(topic, payload):
    if topic == "audit.recorded":
        return "observability"
    if topic == "supervisor.enrolled" or (
        topic == "task.changed" and payload.get("state") == "READY"
    ):
        return "model"
    return {
        "world.requested": "world",
        "compute.requested": "compute",
        "supervisor.task.expired": "maintenance",
        "recovery.requested": "maintenance",
    }.get(topic, "domain")


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    schema_version: int
    trace_id: str
    causation_id: str | None
    correlation_id: str
    work_id: str | None
    branch_id: str | None
    task_id: str | None
    generation: int | None
    world_ref: dict | None
    producer: str
    created_at: str
    payload_ref: str
    sensitivity: str
    routing_key: str

    @classmethod
    def from_source(cls, row):
        # Never include arbitrary payload keys, strings, scope names, or topic names.
        event_id = str(row["outbox_id"])
        p = row["payload"] if isinstance(row["payload"], dict) else {}
        topic = row["topic"] if row["topic"] in TOPICS else "domain.changed"
        wr = p.get("world_ref")
        if isinstance(wr, dict):
            wr = {k: _uuid(wr.get(k)) for k in ("work_id", "branch_id", "world_revision_id")}
            if not all(wr.values()):
                wr = None
        else:
            wr = None
        work = _uuid(row.get("task_work")) or (wr or {}).get("work_id") or _uuid(p.get("work_id"))
        branch = (
            _uuid(row.get("task_branch"))
            or (wr or {}).get("branch_id")
            or _uuid(p.get("branch_id"))
        )
        task = _uuid(row["aggregate_id"] if row["aggregate_type"] == "task" else p.get("task_id"))
        generation = p.get("generation")
        if type(generation) is not int or not 0 <= generation < 2**63:
            generation = None
        trace = _uuid(p.get("trace_id")) or task or event_id
        return cls(
            event_id,
            topic,
            1,
            trace,
            _uuid(p.get("causation_id")),
            _uuid(p.get("correlation_id")) or trace,
            work,
            branch,
            task,
            generation,
            wr,
            "nf.outbox",
            row["created_at"].astimezone(UTC).isoformat(),
            "outbox:" + event_id,
            "reference_only",
            route(topic, p),
        )

    def encode(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


class Streams:
    def __init__(self, client):
        self.client = client
        self._cursors = {}

    def register(self, key, group):
        if group not in SUBSCRIBERS[key]:
            raise ValueError("Unknown subscription")
        try:
            self.client.xgroup_create(STREAMS[key], group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if str(exc).split(" ")[0] != "BUSYGROUP":
                raise

    def send(self, envelope):
        return self.client.xadd(STREAMS[envelope.routing_key], {"envelope": envelope.encode()})

    def messages(self, key, group, worker, count, idle):
        return self.read_batch(key, group, worker, count, idle)[0]

    def read_batch(self, key, group, worker, count, idle):
        self.register(key, group)
        stream = STREAMS[key]
        cursor_key = (key, group, worker)
        result = self.client.xautoclaim(
            stream, group, worker, idle, start_id=self._cursors.get(cursor_key, "0-0"), count=count
        )
        self._cursors[cursor_key] = result[0]
        claimed = list(result[1])
        reclaimed = len(claimed)
        if len(claimed) < count:
            fresh = self.client.xreadgroup(group, worker, {stream: ">"}, count=count - len(claimed))
            for _, entries in fresh:
                claimed.extend(entries)
        return claimed, reclaimed
