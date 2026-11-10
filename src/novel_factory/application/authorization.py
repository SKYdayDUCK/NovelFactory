"""Authenticated identity only; permissions always come from PostgreSQL."""
from dataclasses import dataclass

@dataclass(frozen=True)
class Principal:
    grant_id: str
    principal_id: str

ADMIN_CAPABILITIES = ["READ", "GRAPH_READ", "PLAN_READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM",
                      "PROPOSE", "APPROVE", "ACCEPT", "BRANCH", "REPAIR", "PROMOTE", "ARTIFACT_WRITE", "ADMIN"]
