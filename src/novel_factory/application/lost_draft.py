"""Explicit administrator replacement of one irrecoverable Flash draft return."""
from dataclasses import dataclass

from .lost_review import _replace_lost_handoff


@dataclass(frozen=True)
class LostDraftRequest:
    work_id: str
    branch_id: str
    attempt_id: str
    expected_generation: int
    expected_board_sha256: str
    acknowledge_unknown_cost: bool = False


def replace_lost_draft(kernel, gateway, principal, request: LostDraftRequest, *, apply=False):
    """Enroll a new task, fencing late returns and retaining the old unknown charge."""
    return _replace_lost_handoff(kernel, gateway, principal, request, apply=apply, draft=True)
