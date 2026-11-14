"""Malformed administrative identities fail before any database or model port."""
import pytest

from novel_factory.application.writer_format_recovery import recover_writer_format
from novel_factory.domain.errors import DomainError


@pytest.mark.parametrize("field,value", [("source_revision", "not-a-commit"),
                                         ("expected_board_sha256", "z" * 64),
                                         ("task_id", "not-a-uuid"), ("generation", 0)])
def test_writer_derivation_command_requires_fixed_typed_identity(field, value):
    identifier = "11111111-1111-4111-8111-111111111111"
    args = {"task_id": identifier, "generation": 1, "expected_board_sha256": "a" * 64,
            "idempotency_key": "derive", "source_revision": "a" * 40}
    args[field] = value
    with pytest.raises(DomainError) as denied:
        recover_writer_format(None, None, None, dict.fromkeys(
            ("work_id", "branch_id", "world_revision_id"), identifier), **args)
    assert denied.value.code == "INVALID_ARGUMENT"
