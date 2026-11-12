"""Native S3 manuscript revision, re-reading and editorial decisions on PostgreSQL."""
from datetime import UTC, datetime, timedelta

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.editorial import ReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.review_independence import ReviewIndependenceService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from editorial_helpers import binding_for, decision_request, uid
from test_agent_gateway import FixtureAdapter
from test_release import accept_draft, setup_batch, write_scene
from test_review_independence import enroll_native_case

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
LINE = "她把铜印按在封泥上，等最后一条船靠岸。"
CHANGE = "这一次，她没有抬头。"


def _runtime(k, f, role, kind, inputs):
    gateway = AgentGateway(k, {"fixture": FixtureAdapter()}, creative_harness=CreativeHarnessConfig(enabled=True))
    return RuntimeTaskService(k, gateway).prepare(f["principal"], f["world_ref"], role_id=role,
        task_kind=kind, story_time=point(3), subject_ids=[f["ids"]["sword"]], predicates=["lifecycle"],
        assignment="按原文定位审阅意见，保留未决选择。", adapter_id="fixture", input_artifacts=inputs)


def _args(job):
    return job.principal, job.task["task_id"], job.task["generation"]


def _reviewer(k, f, manuscript, tmp_path, monkeypatch, mode="literary", binding=None, *, editor_group=None):
    b = binding or binding_for(f, [manuscript])
    job = _runtime(k, f, "reviewer", mode, b["ordered_draft_refs"] + b["reconciliation_refs"])
    _, tid, generation = _args(job)
    NarrativeService(k).bind_reader_task(f["principal"], tid, generation, b["ordered_draft_refs"], "bind:" + tid)
    auth = dict(manuscript_ref=manuscript["draft_ref"], reviewer_task_id=tid, reviewer_generation=generation)
    enroll_native_case(k, tmp_path, monkeypatch, **auth, controller=f["principal"])
    if editor_group is not None:
        enroll_native_case(k, tmp_path, monkeypatch, **auth, controller=f["principal"], editor_group=editor_group)
    proof = ReviewIndependenceService(k).issue(f["principal"], **auth)["independence_ref"]
    svc = CreativeReviewService(k)
    read = svc.read(*_args(job), draft_ref=manuscript["draft_ref"], idempotency_key="read", worker_id=job.worker_id)
    assert read["text"] == manuscript["text"]
    return job, dict(binding=b, target_ref=manuscript["draft_ref"], scope_draft_refs=[manuscript["draft_ref"]],
        coverage_refs=[read["coverage_ref"]], layer="PROSE", observations=[], rework_limit=1,
        quota={"budget_id": k.tasks.create_budget(f["world_ref"]["work_id"], "1"), "currency":"USD",
               "max_amount":"0.1", "max_tokens":50000, "max_attempts":6,
               "deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()},
        independence_ref=proof, idempotency_key="review", worker_id=job.worker_id), read


def _repeat(read):
    length = len(LINE)
    return {"pattern": "MECHANICAL_CADENCE", "start": 2*length, "end": 3*length, "reason": "第三处未改变前两处的意义。",
        "counterinterpretation": "三次相同的句子也可以是有意的哀歌。", "requested_change": "只改第三处。",
        "narrative_function": "前两处建立等待，最后一处容许动作转折。", "disposition": "PREFERENCE",
        "related_spans": [{"text_ref": read["text_ref"], "start": i*length, "end": (i+1)*length} for i in (0,1)]}


def _formal(k, f, svc, job, args, read, report, *, retain=False):
    binding = args["binding"]
    # Native STRUCTURE decisions require independent whole-volume reading in addition to a local prose review.
    # Re-review of a directed draft has its own parent layer, so use a separate native whole-volume reader.
    from editorial_helpers import make_review_task, review_request
    native = ReviewService(k)
    reader, task = make_review_task(k, f, binding, "literary")
    delivered = [native.read_text(reader, task["task_id"], task["generation"], ref,
                                  idempotency_key="full:" + ref["artifact_revision_id"]) for ref in binding["ordered_draft_refs"]]
    volume = native.submit_review(reader, review_request(binding, task, [item["coverage_ref"] for item in delivered],
                                                         "VOLUME", "LITERARY", binding["ordered_draft_refs"]))
    session = native.start_editing(f["principal"], {"schema_version":"2.1","binding":binding,"max_rounds":3,
                                "max_issue_decisions":16,"idempotency_key":"edit:"+uid()})
    fn = [{"draft_ref": item["draft_ref"], "function": "人物留下一个未解决的决定。",
           "choice_evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": item["text_ref"], "span": item["span"]}]}
          for item in delivered]
    retained = [{"issue_id": i["finding_id"], "action":"RETAIN_WITH_REASON","reason": "保留等待的重复句。",
                 "evidence_refs":[],"replacement_draft_refs":[]} for i in report["findings"]] if retain else []
    return decision_request(session, None, "STRUCTURE", binding, [report["native_review_ref"],volume], fn,
                            issue_decisions=retained)


def _writer(k, f, source, ref):
    job = _runtime(k, f, "writer", "targeted_revision",
                   [f["plan_ref"],f["batch_ref"],f["scene_refs"][0],source["draft_ref"],ref])
    owner, tid, gen = _args(job)
    PlanningService(k).bind_task_for_controller(f["principal"],tid,gen,f["plan_ref"],
                                               f["scenes"][0]["plan_node_ids"],"plan:"+tid)
    ReleaseService(k).bind_writer_task(owner,tid,gen,f["scene_refs"][0],[],"writer:"+tid)
    NarrativeService(k).bind_reader_task(f["principal"],tid,gen,[source["draft_ref"]],"read:"+tid)
    return job


@pytest.fixture
def manuscript(kernel_env):
    k = kernel_env["kernel"]
    f = setup_batch(k, count=1)
    return k, f, accept_draft(k, f, write_scene(k, f, 0, LINE * 3))


def test_directed_revision_and_independent_clean_reread(manuscript, tmp_path, monkeypatch):
    k, f, source = manuscript
    svc = CreativeReviewService(k)
    job, args, read = _reviewer(k, f, source, tmp_path, monkeypatch)
    args["observations"] = [_repeat(read)]
    report = svc.review(*_args(job), **args)
    assert report["report"]["recommendation"] == "REVISE"
    assert report == svc.review(*_args(job), **args)
    assert report["findings"][0]["counterinterpretation"] == args["observations"][0]["counterinterpretation"]
    def decide(owner, action):
        return svc.decide(owner,*_args(job)[1:],report_ref=report["report_ref"],action=action,
                          reason="一处定向改写，随后回读。",idempotency_key=action)
    with pytest.raises(DomainError):
        decide(job.principal, "RETURN")
    assert decide(f["principal"], "DEFER")["native_decision_ref"] is None
    assert decide(f["principal"], "RETURN")["action"] == "RETURN"
    writer = _writer(k, f, source, report["report_ref"])
    revision = dict(report_ref=report["report_ref"], scene_ref=f["scene_refs"][0], worker_id=writer.worker_id,
        replacements=[{"finding_id": report["findings"][0]["finding_id"], "replacement": CHANGE}], idempotency_key="replace")
    with pytest.raises(DomainError):
        svc.revise(*_args(writer), **{**revision, "replacements":[{"finding_id":uid(),"replacement":"越界"}]})
    child = svc.revise(*_args(writer), **revision)
    assert svc.revise(*_args(writer), **revision) == child
    with pytest.raises(DomainError) as stale:
        decide(f["principal"], "WITHDRAW")
    assert stale.value.code == "TASK_REBIND_REQUIRED"
    accepted = accept_draft(k,f,{"writer":writer.principal,"task":writer.task,"draft_ref":child["draft_ref"],"text":LINE*2+CHANGE})
    newjob, newargs, newread = _reviewer(k, f, accepted, tmp_path, monkeypatch, editor_group="volume-editors")
    with pytest.raises(DomainError) as wrong:
        svc.review(*_args(newjob), **newargs)
    assert wrong.value.code == "INVALID_EVIDENCE"
    newargs["prior_report_ref"] = report["report_ref"]
    with pytest.raises(DomainError) as reset:
        svc.review(*_args(newjob), **newargs)
    assert reset.value.code == "BUDGET_EXCEEDED"
    newargs["quota"] = args["quota"]
    reread = svc.review(*_args(newjob), **newargs)
    assert not reread["findings"] and reread["report"]["recommendation"] == "ACCEPT_CANDIDATE"
    final_args = dict(report_ref=reread["report_ref"], action="ACCEPT_REVISION", reason="第三处动作改变，前两处保留。",
                      idempotency_key="accept",native_decision=_formal(k,f,svc,newjob,newargs,newread,reread))
    result = svc.decide(f["principal"],*_args(newjob)[1:],**final_args)
    with k.db.uow(write=False) as repo:
        assert k.artifact(repo,result["native_decision_ref"],f["world_ref"]["work_id"],kind="EDITORIAL_DECISION")
        assert k.artifact(repo,reread["details_ref"],f["world_ref"]["work_id"])["payload"]["round"] == 1
        original = k.artifact(repo,source["draft_ref"],f["world_ref"]["work_id"])["payload"]
        assert k.objects.read(original["text_ref"]["sha256"]).decode() == LINE*3
    assert svc.decide(f["principal"],*_args(newjob)[1:],**final_args) == result


def test_withdraw_retain_and_rework_limit(manuscript, tmp_path, monkeypatch):
    k, f, source = manuscript
    svc = CreativeReviewService(k)
    job,args,read = _reviewer(k,f,source,tmp_path,monkeypatch,editor_group="volume-editors")
    args.update(observations=[_repeat(read)], rework_limit=0)
    limited = svc.review(*_args(job), **args)
    def decide(owner, action, result, **kwargs):
        return svc.decide(owner,*_args(job)[1:],report_ref=result["report_ref"],action=action,reason="保留判断与理由。",
                          idempotency_key=action + result["report_ref"]["artifact_revision_id"],**kwargs)
    with pytest.raises(DomainError) as cap:
        decide(f["principal"],"RETURN",limited)
    assert cap.value.code == "REWORK_LIMIT"
    invalid = _formal(k,f,svc,job,args,read,limited)
    with pytest.raises(DomainError) as missing:
        decide(f["principal"],"RETAIN",limited,native_decision=invalid)
    assert missing.value.code == "EDITORIAL_DECISION_REQUIRED"
    formal = _formal(k,f,svc,job,args,read,limited,retain=True)
    assert decide(f["principal"],"RETAIN",limited,native_decision=formal)["native_decision_ref"]
    withdrawn = svc.review(*_args(job),**{**args,"idempotency_key":"different-reading","rework_limit":1})
    decide(f["principal"], "RETURN",withdrawn)
    assert decide(job.principal, "WITHDRAW",withdrawn,worker_id=job.worker_id)["action"] == "WITHDRAW"
    writer = _writer(k,f,source,withdrawn["report_ref"])
    with pytest.raises(DomainError) as ended:
        svc.revise(*_args(writer),report_ref=withdrawn["report_ref"],scene_ref=f["scene_refs"][0],
                   replacements=[{"finding_id":withdrawn["findings"][0]["finding_id"],"replacement":CHANGE}],idempotency_key="stop")
    assert ended.value.code == "REVIEW_REQUIRED"


def test_hard_conflict_requires_native_assertion_check(kernel_env, tmp_path, monkeypatch):
    from novel_factory.fixtures import entity_value
    from test_narrative import assertion, extract

    k = kernel_env["kernel"]
    f = setup_batch(k, count=1)
    draft = write_scene(k, f, 0, "那把剑从来不是铁匠的。")
    check = {**f["scenes"][0]["entry_checks"][0], "expected_value":entity_value(f["ids"]["protagonist"])}
    extracted = extract(k, f, {**draft,"scene":f["scenes"][0]},
                        [assertion(draft["text"], classification="HISTORICAL_CONFLICT", world_check=check)])
    blocked = NarrativeService(k).reconcile(f["principal"], draft["draft_ref"], extracted["extraction_ref"],
                                             f["world_ref"], "contradiction-check")
    assert blocked["status"] == "BLOCKED"
    binding = binding_for(f, [{**draft,"reconciliation_ref":blocked["reconciliation_ref"],"acceptance_ref":None}])
    binding["ordered_acceptance_refs"] = []
    job,args,read = _reviewer(k, f, draft, tmp_path, monkeypatch, mode="fact", binding=binding)
    args["layer"] = "FACT_KNOWLEDGE"
    svc = CreativeReviewService(k)
    result = svc.review(*_args(job),**args)
    finding, = result["report"]["findings"]
    assert finding["dimension"] == "FACT" and finding["judgment"] == "CONTRADICTION"
    assert {"kind":"ARTIFACT","artifact_ref":blocked["reconciliation_ref"]} in finding["evidence_refs"]
    assert finding["text_span"] == span_for(read["text"])
    forged = {**_repeat(read),"pattern":"POV_LEAK", "start":0, "end":len(read["text"]),
              "related_spans":[],"disposition":"QUESTION"}
    with pytest.raises(DomainError) as crossed:
        svc.review(*_args(job), **{**args,"observations":[forged],"idempotency_key":"other-plane"})
    assert crossed.value.code == "VIEW_DENIED"


def test_review_cycle_cannot_exceed_financial_or_wall_limits(manuscript, tmp_path, monkeypatch):
    k, f, source = manuscript
    svc = CreativeReviewService(k)
    job,args,read = _reviewer(k,f,source,tmp_path,monkeypatch)
    for changes, code in [({"max_amount":"5"},"BUDGET_EXCEEDED"), ({"max_tokens":1},"BUDGET_EXCEEDED"),
                          ({"deadline":(datetime.now(UTC)-timedelta(minutes=1)).isoformat()},"DEADLINE_EXCEEDED")]:
        with pytest.raises(DomainError) as denied:
            svc.review(*_args(job), **{**args,"quota":{**args["quota"], **changes}})
        assert denied.value.code == code
    args["observations"] = [_repeat(read)]
    from novel_factory.domain.canonical import canonical_bytes
    args["quota"]["max_tokens"] = len(canonical_bytes(args["observations"])) + len(read["text"].encode())
    report = svc.review(*_args(job),**args)
    svc.decide(f["principal"],*_args(job)[1:],report_ref=report["report_ref"],action="RETURN",reason="精简第三处。",
               idempotency_key="return")
    writer = _writer(k,f,source,report["report_ref"])
    with pytest.raises(DomainError) as no_more:
        svc.revise(*_args(writer),report_ref=report["report_ref"],scene_ref=f["scene_refs"][0],
                   replacements=[{"finding_id":report["findings"][0]["finding_id"],"replacement":CHANGE}],idempotency_key="over")
    assert no_more.value.code == "BUDGET_EXCEEDED"


def test_seam_requires_both_live_producer_groups_and_manifest_order(kernel_env, tmp_path, monkeypatch):
    k = kernel_env["kernel"]
    f = setup_batch(k, count=2)
    sources = [accept_draft(k,f,write_scene(k,f,i,LINE)) for i in range(2)]
    binding = binding_for(f,sources)
    svc = CreativeReviewService(k)
    job,args,read = _reviewer(k,f,sources[1],tmp_path,monkeypatch,mode="continuity",binding=binding)
    first, second = binding["ordered_draft_refs"]
    args.update(layer="CHAPTER_SEAM",scope_draft_refs=[first,second])
    with pytest.raises(DomainError) as missing:
        svc.review(*_args(job),**args)
    assert missing.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    source_args = dict(manuscript_ref=first,reviewer_task_id=job.task["task_id"],
                       reviewer_generation=job.task["generation"])
    case = enroll_native_case(k,tmp_path,monkeypatch,**source_args,controller=f["principal"])
    secondary = ReviewIndependenceService(k).issue(f["principal"],**source_args)["independence_ref"]
    args["related_independence_refs"] = [secondary]
    args["coverage_refs"].append(svc.read(*_args(job),draft_ref=first,idempotency_key="first")["coverage_ref"])
    with pytest.raises(DomainError) as reversed_order:
        svc.review(*_args(job),**{**args,"scope_draft_refs":[second,first]})
    assert reversed_order.value.code == "INVALID_ARGUMENT"
    result = svc.review(*_args(job),**args)
    assert result["findings"][0]["pattern"] == "REPEATED_SEAM"
    assert result["findings"][0]["quote"] == LINE
    final = dict(report_ref=result["report_ref"], action="RETAIN", reason="有意保留衔接重复。",
                 idempotency_key="retain-seam", native_decision=_formal(k, f, svc, job, args, read, result, retain=True))
    enroll_native_case(k, tmp_path, monkeypatch, **{**source_args, "manuscript_ref": second},
                       controller=f["principal"], editor_group="volume-editors")
    with pytest.raises(DomainError, match="qualified"):
        svc.decide(f["principal"], *_args(job)[1:], **final)
    enroll_native_case(k, tmp_path, monkeypatch, **source_args, controller=f["principal"], editor_group="other-editors")
    with pytest.raises(DomainError, match="qualified"):
        svc.decide(f["principal"], *_args(job)[1:], **final)
    enroll_native_case(k, tmp_path, monkeypatch, **source_args, controller=f["principal"], editor_group="volume-editors")
    retained = svc.decide(f["principal"], *_args(job)[1:], **final)
    with k.db.uow(write=False) as repo:
        decision = k.artifact(repo, retained["decision_ref"], f["world_ref"]["work_id"])["payload"]
        assert [entry["manuscript_ref"] for entry in decision["editor_independence"]] == [second, first]
    case.unlink()
    with pytest.raises(DomainError) as lost:
        svc.review(*_args(job),**args)
    assert lost.value.code == "INDEPENDENT_REVIEW_REQUIRED"
    with pytest.raises(DomainError):
        svc.decide(f["principal"],*_args(job)[1:],report_ref=result["report_ref"],action="DEFER",
                   reason="新授权待定。",idempotency_key="defer")
