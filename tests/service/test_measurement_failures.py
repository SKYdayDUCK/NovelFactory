"""Failed SQL waits remain visible in measured capacity evidence."""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from benchmarks.http_load import SqlMeasurements

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_statement_timeout_is_counted_once_with_its_wait(service_env):
    engine = service_env['kernel'].db.engine
    with SqlMeasurements(engine) as meter:
        with pytest.raises(DBAPIError):
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL statement_timeout = '10ms'"))
                conn.execute(text('SELECT pg_sleep(1)'))
        result = meter.result(1)
    assert result['sql_statements'] == 2
    assert result['failed_statements'] == 1
    assert result['sql_cursor_execution_seconds'] >= .005
    assert result['sql_timing_includes_failed_cursor_executions'] is True
    assert result['checked_out_at_end'] == 0
