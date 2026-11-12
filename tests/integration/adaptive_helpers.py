"""A disjoint native S3 training and held-out pair, with signed independent reading cases."""

import json
from datetime import UTC, datetime, timedelta

from test_harness_review_workflow import _args, _reviewer
from test_release import accept_draft, setup_batch, write_scene

from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig


def setup_adaptive_case(kernel, tmp_path, monkeypatch):
    config = CreativeHarnessConfig(enabled=True, adaptive_instructions=True)
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(config.as_dict()))
    world = setup_batch(kernel, count=4)
    wr, admin = world["world_ref"], world["principal"]
    records = []
    lines = [
        "她把铜印按在封泥上，等最后一条船靠岸。",
        "日落以后，港口才真正安静。",
        "她提起铁壶，又听见门外响起脚步。",
    ]
    second = "她把伞收在身后，抬头看见门框上发白的划痕。"
    texts = [
        lines[0] * 3,
        lines[1],
        lines[2] * 3 + second * 3,
        "秦生掀开门帘，认出衣角上那点熟悉的蓝。",
    ]
    for index, text in enumerate(texts):
        original = accept_draft(kernel, world, write_scene(kernel, world, index, text))
        job, args, read = _reviewer(kernel, world, original, tmp_path, monkeypatch)
        obs = {
            "reason": "对照三次等待的动作和最后的转折。",
            "counterinterpretation": "重复也可能表现哀歌。",
            "narrative_function": "观察人物等待的方式。",
            "related_spans": [],
            "severity": "MINOR",
        }
        observations = [obs]
        if index in (0, 2):
            n = len(lines[index])
            obs.update(
                pattern="MECHANICAL_CADENCE",
                start=2 * n,
                end=3 * n,
                disposition="PREFERENCE",
                requested_change="检查第三次重复是否增加新信息。",
                related_spans=[
                    {"text_ref": read["text_ref"], "start": i * n, "end": (i + 1) * n}
                    for i in (0, 1)
                ],
            )
            if index == 2:
                offset, m = 3 * n, len(second)
                observations.append(
                    {
                        **obs,
                        "start": offset + 2 * m,
                        "end": offset + 3 * m,
                        "related_spans": [
                            {
                                "text_ref": read["text_ref"],
                                "start": offset + i * m,
                                "end": offset + (i + 1) * m,
                            }
                            for i in (0, 1)
                        ],
                    }
                )
        else:
            obs.update(
                pattern="MECHANICAL_CADENCE",
                start=0,
                end=len(text),
                disposition="PRESERVE",
                requested_change=None,
                counterinterpretation="此处的复述是对前一句话的模仿，应保留。",
            )
        report = CreativeReviewService(kernel).review(
            *_args(job), **{**args, "observations": observations}
        )
        records.append((job, report["report_ref"], args, original))
    source = records[0][0]
    args = {
        "task_id": source.task["task_id"],
        "generation": source.task["generation"],
        "failure_report_refs": [records[0][1]],
        "success_report_refs": [records[1][1]],
        "evaluation_report_refs": [records[2][1], records[3][1]],
        "change_kind": "REVIEW_RUBRIC",
        "quota": {
            "currency": "USD",
            "max_amount": "0.1",
            "max_tokens": 180000,
            "max_attempts": 24,
            "budget_id": kernel.tasks.create_budget(wr["work_id"], "2"),
            "deadline": (datetime.now(UTC) + timedelta(minutes=8)).isoformat(),
        },
        "order_seed": 2048,
        "repetitions": 1,
        "idempotency_key": "registered-reading-method",
    }
    return {
        "kernel": kernel,
        "world": world,
        "wr": wr,
        "admin": admin,
        "source": source,
        "failure_ref": records[0][1],
        "success_ref": records[1][1],
        "evaluation_report_refs": args["evaluation_report_refs"],
        "records": records,
        "args": args,
    }
