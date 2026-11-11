"""One session-scoped LISTEN connection per service, outside its task pool."""
from contextlib import contextmanager

import psycopg


@contextmanager
def wake_listener(engine):
    dsn = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(dsn, autocommit=True, application_name="nf_supervisor_listener") as conn:
        conn.execute("LISTEN nf_supervisor_wake")
        # LISTEN has committed before the first durable scan. A notification
        # that arrives during that scan remains queued for the next wait.
        yield conn


def wait_for_change(listener, timeout):
    notices = listener.notifies(timeout=timeout, stop_after=1)
    try:
        return next(notices, None) is not None
    finally:
        notices.close()
