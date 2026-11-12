import pytest

from novel_factory.application.scion import ScionOptions


@pytest.mark.parametrize("group,overrides,expected", [
    ("0", {"WORKER": "1"}, (True, False, False)),
    ("1", {"REVIEW": "0"}, (True, False, True)),
    ("0", {"QUERY": "1"}, (False, False, True)),
    ("1", {"WORKER": "0", "QUERY": "0"}, (False, True, False)),
])
def test_column_switches_allow_independent_rollout_and_rollback(monkeypatch, group, overrides, expected):
    monkeypatch.setenv("NF_SCION_COLUMNS", group)
    for name in ("WORKER", "REVIEW", "QUERY"):
        key = f"NF_SCION_{name}_COLUMNS"
        if name in overrides:
            monkeypatch.setenv(key, overrides[name])
        else:
            monkeypatch.delenv(key, raising=False)
    options = ScionOptions()
    assert (options.worker_columns, options.review_columns, options.query_columns) == expected
    assert options.columns == all(expected)
    options.columns = False
    assert not any((options.worker_columns, options.review_columns, options.query_columns))


def test_default_enables_only_worker_metadata(monkeypatch):
    for name in ("COLUMNS", "WORKER_COLUMNS", "REVIEW_COLUMNS", "QUERY_COLUMNS", "SCOPES", "REPLAY"):
        monkeypatch.delenv("NF_SCION_" + name, raising=False)
    options = ScionOptions()
    assert options.worker_columns
    assert not any((options.review_columns, options.query_columns, options.scopes, options.replay))
