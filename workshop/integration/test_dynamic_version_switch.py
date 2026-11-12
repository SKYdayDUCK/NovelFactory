"""Independent W4 skill releases and the NF selection applied to each actual provider request."""

from test_dynamic_skill_dispatch import call, dynamic_site as employee_site  # noqa: F401


def test_running_draft_keeps_prior_version_and_rollback_changes_only_future_tasks(employee_site):  # noqa: F811
    site = employee_site
    source = site.delegate("version-source")
    first_text = "Find the oar that strikes the hull before explaining why the passenger hesitates."
    second_text = "Find the hand that shields the flame before describing why the watchman waits."
    first_profile = "Prefer the passenger's visible choice."
    second_profile = "Prefer the watchman's visible action."
    first = site.activate(
        source, site.share("p1", first_text), first_text, profile="p1", role_text=first_profile
    )
    instance = first["instance_ref"]
    earlier_task = site.delegate("task-p1", instance)
    later = site.activate(
        source, site.share("p2", second_text), second_text, profile="p2", role_text=second_profile
    )
    assert later["instance_ref"] == instance and later["revision_ref"] != first["revision_ref"]
    later_task = site.delegate("task-p2", instance)

    def history():
        return call(site.nf_controller, "nf_harness_agent_history", world_ref=site.wr, instance_ref=instance)

    selected = history()
    result = call(
        site.nf_controller,
        "nf_harness_agent_select",
        world_ref=site.wr,
        instance_ref=instance,
        revision_ref=first["revision_ref"],
        expected_epoch=selected["epoch"],
        idempotency_key="rollback-p1",
    )
    assert result["revision_ref"] == first["revision_ref"]
    reverted_task = site.delegate("task-rollback", instance)

    for task, profile, skill, absent in (
        (earlier_task, first_profile, first_text, second_text),
        (later_task, second_profile, second_text, first_text),
        (reverted_task, first_profile, first_text, second_text),
    ):
        output = site.gateway.execute(task.principal, task.request)
        assert output["status"] == "RESULT_STORED", output
        system = site.requests[-1]["messages"][0]["content"]
        assert profile in system and skill in system and absent not in system
    assert len(site.requests) == 3
    assert [entry["revision_ref"] for entry in history()["events"]] == [
        first["revision_ref"],
        later["revision_ref"],
        first["revision_ref"],
    ]
    with site.kernel.db.uow(write=False) as repo:
        assert site.kernel.head(repo, site.wr["work_id"], site.wr["branch_id"]) == site.wr
        for task in (earlier_task, later_task, reverted_task):
            assert len(repo.many("call_attempts", {"task_id": task.task_id})) == 1
