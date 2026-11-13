from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from novel_factory.application.status_coalescing import StatusCoalescer


def test_duplicate_callers_share_one_fresh_read_and_late_arrivals_reread():
    gate = StatusCoalescer(window=.01, max_members=2)
    started, release = Event(), Event()
    rendezvous = Barrier(2)
    key = ("grant", "work", "branch", "task")

    def before_rebind():
        started.set()
        assert release.wait(5)
        return "old"

    def lane():
        rendezvous.wait(5)
        return gate.read(key, before_rebind)

    with ThreadPoolExecutor(3) as pool:
        readers = [pool.submit(lane) for _ in range(2)]
        try:
            assert started.wait(5)
            assert gate.read(key, lambda: "new") == "new"
            assert gate.read(("other-grant", *key[1:]), lambda: "denied") == "denied"
        finally:
            release.set()
        assert [r.result() for r in readers] == ["old", "old"]
    assert gate.joined == 1


def test_error_is_never_cached():
    gate = StatusCoalescer()
    try:
        gate.read("revoked", lambda: int("invalid"))
    except ValueError:
        pass
    assert gate.read("revoked", lambda: "current") == "current"
