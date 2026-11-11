"""Run identical protocol traces on two physical PostgreSQL 17 database clones.

Reuse the existing public-service scenarios and their independent assertions.
Compare every application table, including complete nested JSON and identities.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from collections import defaultdict
import importlib
import itertools
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

import test_continuity as continuity
import test_derived_worker as worker
import test_history_repair as repair
import test_runtime_upgrades as upgrades
import test_world_kernel as world_tests
from concurrency import test_world_races as races
from queries import test_audit_regressions as access_audit
from queries import test_queries as query_tests
from novel_factory import fixtures
from novel_factory.application import kernel as kernel_module
from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.canonical import canonical_bytes
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import entity_value, point, prepare_command
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.repository import KernelRepository
from novel_factory.orchestration.tasks import TaskService

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
FLAGS = ("NF_SCION_COLUMNS", "NF_SCION_SCOPES", "NF_SCION_REPLAY")
# Database bookkeeping timestamps. Presence is checked, and all timestamps
# embedded in contracts, incl. receipt/approval/expiry, remain exact.
BOOKKEEPING = {"created_at", "updated_at", "processed_at", "delivered_at"}


@contextmanager
def clone(template, directory):
    base = make_url(template["url"])
    name = "nf_diff_" + uuid4().hex
    admin = template["admin"]
    with admin.connect() as conn:
        assert int(conn.scalar(text("SHOW server_version_num"))) // 10000 == 17
        quote = conn.dialect.identifier_preparer.quote_identifier
        conn.exec_driver_sql(f"CREATE DATABASE {quote(name)} TEMPLATE {quote(base.database)}")
    db = Database(base.set(database=name).render_as_string(hide_password=False), pool_size=8)
    try:
        kernel = Kernel(db, ObjectStore(directory))
        yield {"db": db, "kernel": kernel, "query": WorldQueryService(kernel)}
    finally:
        db.close()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')


def pin_inputs(patch, now, scenario):
    # Keep entropy deterministic only at this fixture's input boundaries.
    # PostgreSQL still executes all locks, constraints, statements and commits.
    modules = [
        "novel_factory.fixtures", "novel_factory.orchestration.tasks",
        *("novel_factory.application." + name for name in
          ("kernel", "queries", "continuity", "upgrades", "release", "editorial", "narrative", "planning")),
        "editorial_helpers", "test_history_repair", "test_continuity", "test_release",
        "test_editorial_service", "test_narrative", "test_planning", "queries.conftest",
        races.__name__,
    ]

    class FixedDate(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    def ids(label):
        stream = itertools.count()
        return lambda: uuid5(NAMESPACE_URL, f"nf-diff:{scenario}:{label}:{next(stream)}")

    for name in modules:
        module = importlib.import_module(name)
        if hasattr(module, "uuid4"):
            patch.setattr(module, "uuid4", ids(name))
        if getattr(module, "datetime", None) is datetime:
            patch.setattr(module, "datetime", FixedDate)
    tokens = ids("test-only-token")
    patch.setattr(kernel_module, "secrets", SimpleNamespace(token_urlsafe=lambda size: tokens().hex))
    patch.setattr(KernelRepository, "now", lambda repo: now)
    patch.setattr(TaskService, "_now", staticmethod(lambda conn: now))


def observe_public_results(patch):
    returns = defaultdict(list)
    def observe(cls, method):
        bound = getattr(cls, method)
        @wraps(bound)
        def wrapper(self, *args, **kwargs):
            value = bound(self, *args, **kwargs)
            body = value if isinstance(value, bytes) else canonical_bytes(value)
            returns[method].append(sha256(body).hexdigest())
            return value
        patch.setattr(cls, method, wrapper)

    for name in ("accept", "propose", "prepare", "approve", "compare", "impact", "get_artifact"):
        observe(Kernel, name)
    for name in ("query", "context_compile"):
        observe(WorldQueryService, name)
    return returns


def table_fingerprints(kernel):
    result = {}
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        for name, table in sorted(tables.items()):
            rows = repo.conn.execute(select(table).order_by(*table.primary_key.columns)).mappings()
            digest = sha256()
            count = 0
            for row in rows:
                values = {key: (val is not None if key in BOOKKEEPING else val) for key, val in row.items()}
                encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str).encode()
                digest.update(len(encoded).to_bytes(8, "big") + encoded)
                count += 1
            result[name] = [count, digest.hexdigest()]
    return result


def history(env):
    repair.test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes(env)
    upgrades.test_upgrade_appends_revision_preserves_history_and_fences_old_certificate(env)
    k = env["kernel"]
    w = fixtures.load_fixture(k)
    races.test_repair_revalidates_later_closed_command_preconditions(k, w)
    w = fixtures.load_fixture(k)
    # Append across a disjoint scope at an earlier story time. It is a valid
    # new commit on this branch, but requires reordering world history.
    later = fixtures.run_command(k, w["principal"], w["world_ref"], "TRANSFER_ITEM",
                                 races._gift(w, day=20), key="late")
    earlier = fixtures.run_command(k, w["principal"], later["world_ref"], "TRANSFER_ITEM",
                                   races._gift(w, "sword2", "smith2", "friend", day=10), key="early")
    branch = k.fork(w["principal"], earlier["world_ref"], "REPAIR", "duplicate-effect-test")
    duplicate = races._replacement(k, branch)
    duplicate["effects"].append(duplicate["effects"][0].copy())
    with pytest.raises(DomainError) as rejected:
        prepare_command(k, w["principal"], branch, "REPAIR_EVENT", {
            "occurrence_id": duplicate["occurrence_id"], "replacement_event": duplicate,
            "reason": "Test repeated effect identity in a version replacing an existing event."},
            reviewer=races._reviewer(k, w))
    assert rejected.value.code == "DUPLICATE_EFFECT"


def published(env):
    continuity.test_retcon_propagates_to_later_volumes_and_requires_fresh_evidence(env)


def authorization(env):
    access_audit.test_non_admin_artifact_must_match_live_task_generation_and_cut(env)
    query_tests.test_cache_hit_rechecks_revoked_grant(env)
    query_tests.test_actor_hidden_and_nonexistent_have_identical_answer_shape(env)


def derived(env):
    worker.test_out_of_order_materialization_never_regresses_watermark(env)
    worker.test_delayed_index_is_explicitly_stale_and_dependency_guard_does_not_wait_for_worker(env)


def contention(env):
    kernel = env["kernel"]
    f = fixtures.fixture_definition("white_bridge")
    for rel in f["genesis"]["relations"]:
        if rel["predicate"] == "located_in" and rel["subject_id"] in {f["ids"]["friend"], f["ids"]["smith2"]}:
            rel["object_value"] = entity_value(f["ids"]["south"])
    f.update(kernel.bootstrap_work("Two regions", f["genesis"], frontiers=f["frontiers"]))
    # Both competing databases must observe actual branch row-lock waits.
    races.test_guarded_independent_items_keep_original_base(kernel, f)
    w = fixtures.load_fixture(kernel)
    first = races._prepare(kernel, w, "TRANSFER_ITEM", races._gift(w))
    second = races._prepare(kernel, w, "TRANSFER_ITEM", races._gift(w, target="friend"))
    win, lose, _ = races._overlap(kernel, w["principal"], first["request"], second["request"])
    assert win["branch_seq"] == 1 and lose.code == "DEPENDENCY_CONFLICT"

    w = fixtures.load_fixture(kernel, "white_bridge")
    agents = [kernel.issue_grant(w["principal"], w["world_ref"]["work_id"],
              role_id="control_operator", capabilities=ADMIN_CAPABILITIES, planes=["WORLD"],
              max_story_time=point(50))["principal"] for _ in range(3)]
    bids = [prepare_command(kernel, agent, w["world_ref"], "RESOURCE_TRANSFER",
                            races._transfer(w, target=target, amount="80"),
                            mode="DEPENDENCY_GUARDED", key="bid-" + str(i))["request"]
            for i, (agent, target) in enumerate(zip(agents, ("relief", "recipient", "reserve")))]
    held, release = threading.Event(), threading.Event()

    def pause(stage):
        if stage == "after_events":
            held.set()
            assert release.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=3) as pool:
        winning = pool.submit(kernel.accept, agents[0], bids[0], fault=pause)
        try:
            assert held.wait(timeout=8)
            losers = [pool.submit(kernel.accept, agent, bid) for agent, bid in zip(agents[1:], bids[1:])]
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with kernel.db.engine.connect() as conn:
                    blocked = conn.scalar(text("""SELECT count(*) FROM pg_stat_activity
                        WHERE datname=current_database() AND cardinality(pg_blocking_pids(pid))>0"""))
                if blocked >= 2:
                    break
                time.sleep(.01)
            assert blocked >= 2
        finally:
            release.set()
        assert winning.result(timeout=8)["branch_seq"] == 1
        for future in losers:
            with pytest.raises(DomainError, match="dependency") as error:
                future.result(timeout=8)
            assert error.value.code == "DEPENDENCY_CONFLICT"
    races.test_negative_range_phantom_invalidates_clear_route(kernel, fixtures.load_fixture(kernel))
    races.test_expired_and_unissued_certificates_are_rejected(kernel, fixtures.load_fixture(kernel))


def faults(env):
    kernel = env["kernel"]
    w = fixtures.load_fixture(kernel, "white_bridge")
    cmd = races._prepare(kernel, w, "RESOURCE_TRANSFER", races._transfer(w))
    # PostgreSQL itself raises inside the real transaction after event insertion.
    with kernel.db.engine.begin() as conn:
        conn.exec_driver_sql("""CREATE FUNCTION nf_diff_fail() RETURNS trigger LANGUAGE plpgsql
            AS $$ BEGIN RAISE EXCEPTION 'injected commit failure' USING ERRCODE='P0001'; END $$""")
        conn.exec_driver_sql("""CREATE TRIGGER nf_diff_fail BEFORE INSERT ON nf_commits
            FOR EACH ROW EXECUTE FUNCTION nf_diff_fail()""")
    before = table_fingerprints(kernel)
    with pytest.raises(DBAPIError) as error:
        kernel.accept(w["principal"], cmd["request"])
    assert error.value.orig.sqlstate == "P0001"
    assert table_fingerprints(kernel) == before
    with kernel.db.engine.begin() as conn:
        conn.exec_driver_sql("DROP FUNCTION nf_diff_fail() CASCADE")
    receipt = kernel.accept(w["principal"], cmd["request"])
    assert kernel.accept(w["principal"], cmd["request"]) == receipt
    world_tests.test_lost_response_then_cancel_retrieves_original_receipt(env)
    world_tests.test_old_generation_and_cancelled_task_cannot_commit(env)


@pytest.mark.parametrize("scenario", [history, published, authorization, derived, contention, faults],
                         ids=lambda fn: fn.__name__)
def test_same_postgres_trace(kernel_env, tmp_path, monkeypatch, scenario):
    # Fixed input across independent processes/commits; outbox delivery leases
    # still come from the real database clock.
    now = datetime(2024, 6, 1, tzinfo=UTC)
    frozen = os.environ.get("NF_SCION_DIFF_FROZEN") == "1"
    if frozen:
        root = Path(__file__).resolve().parents[2]
        expected = "2c28acba6e1932657162c6e55b3288365ce3ea23"
        assert Path(kernel_module.__file__).resolve().is_relative_to(root / "src")
        subprocess.run(["git", "diff", "--exit-code", expected, "--", "src", "migrations"],
                       cwd=root, check=True)
        assert os.environ.get("NF_SCION_DIFF_EVIDENCE")
    modes = {}
    diagnostics = {}
    configurations = {"reference": set(), "candidate": set(FLAGS)}
    worker, review, query = ("NF_SCION_WORKER_COLUMNS", "NF_SCION_REVIEW_COLUMNS", "NF_SCION_QUERY_COLUMNS")
    lazy, links = "NF_SCION_REPLAY_LAZY", "NF_SCION_SCHEMA_LINKS"
    scope, replay = FLAGS[1:]
    if os.environ.get("NF_SCION_DIFF_ROUND2") == "1":
        configurations.update({
            "B_metadata": {worker, review}, "C_scopes": {scope},
            "D_metadata_scopes": {worker, review, scope}, "E_lazy": {replay, lazy},
            "F_worker": {worker},
            "schema_service": {links}, "all_round2": {*FLAGS, lazy, links},
        })
    for mode, active in ({"frozen": set()} if frozen else configurations).items():
        with monkeypatch.context() as patch:
            for flag in (*FLAGS, lazy, links):
                patch.setenv(flag, "1" if flag in active else "0")
            for flag in (worker, review, query):
                patch.setenv(flag, "1" if flag in active or FLAGS[0] in active else "0")
            if not frozen:
                from novel_factory.contracts import registry
                patch.setattr(registry, "_STATIC_SCHEMA_LINKS", links in active)
            pin_inputs(patch, now, scenario.__name__)
            returns = observe_public_results(patch)
            errors = []
            original_error = DomainError.__init__

            def observe_error(error, *args, **kwargs):
                original_error(error, *args, **kwargs)
                errors.append(error.as_dict("fixed-request-id"))

            patch.setattr(DomainError, "__init__", observe_error)
            with clone(kernel_env, tmp_path / mode) as env:
                scenario(env)
                kernel = env["kernel"]
                if frozen:
                    assert not hasattr(kernel, "scion")
                else:
                    assert kernel.scion.worker_columns == (worker in active or FLAGS[0] in active)
                    assert kernel.scion.review_columns == (review in active or FLAGS[0] in active)
                    assert kernel.scion.query_columns == (query in active or FLAGS[0] in active)
                    assert kernel.scion.scopes == (scope in active)
                    assert kernel.scion.replay == (replay in active)
                    assert kernel.scion.replay_lazy == (lazy in active)
                    diagnostics[mode] = kernel.scion.counts()
                modes[mode] = {"tables": table_fingerprints(kernel), "errors": errors,
                               "responses": dict(returns)}
    evidence_dir = os.environ.get("NF_SCION_DIFF_EVIDENCE")
    if evidence_dir:
        folder = Path(evidence_dir)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (scenario.__name__ + ".json")).write_text(json.dumps({**modes, "path_counts": diagnostics}, indent=2) + "\n")
    if frozen:
        return
    oracle_dir = os.environ.get("NF_SCION_DIFF_ORACLE")
    if oracle_dir:
        oracle = json.loads((Path(oracle_dir) / (scenario.__name__ + ".json")).read_text())["frozen"]
        assert modes["reference"] == oracle
    assert modes["reference"]["responses"] == modes["candidate"]["responses"]
    assert diagnostics["reference"] == {}
    if scenario is history:
        for reason in ("replay.hit", "replay.historical_replacement", "scopes.hit", "columns.query_hit"):
            assert diagnostics["candidate"].get(reason, 0) > 0, reason
    assert modes["reference"]["errors"] == modes["candidate"]["errors"]
    for name, expected in modes["reference"]["tables"].items():
        assert modes["candidate"]["tables"][name] == expected, name
    for mode in configurations:
        assert modes[mode] == modes["reference"], mode
    if "E_lazy" in modes and scenario is history:
        assert diagnostics["E_lazy"].get("replay.hit", 0) > 0
        assert diagnostics["E_lazy"].get("replay_proof.cold_proposal_skipped", 0) > 0
