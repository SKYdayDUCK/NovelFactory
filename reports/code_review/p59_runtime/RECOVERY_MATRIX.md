# Recovery and scheduling acceptance

The following rows identify concrete checks. The task, execution lease, result, dispatch slot, runtime permit, workflow event and provider Attempt each have their own identity. An accepted summary is an artifact-level decision. A stored result does not advance the world head.

| Requested condition | Check and observation |
| --- | --- |
| Concurrent task claim | `tests/reliability/test_tasks.py::test_two_connections_cannot_claim_same_task` uses independent PostgreSQL transactions. |
| Old worker after expiry | `test_expired_lease_recovery_fences_old_result` in the same suite rejects stale qualification. `test_renewed_execution_keeps_its_slot_across_dispatch_and_restart` additionally checks the live execution slot. |
| Worker disappears during execution | `test_p5_process_resume.py` and `tests/concurrency/test_world_races.py` exercise subprocess loss, recovery and exactly-once formal effects. |
| Cancellation races a result | `test_cancel_and_result_race_leaves_no_qualification` checks both possible serialization orders. |
| Restart while waiting | `test_registered_wait_wakes_after_restart_without_model_polling` and `test_restore_fences_wait_without_losing_preconditions` cover service reconstruction and backup/restore. A live-process kill and recovery is a separate case. |
| Repeated decision/delivery | `test_durable_barrier_replay_and_manual_bypass` and `test_committed_hint_only_and_duplicate_reconciliation` check consumption identity and duplicate wake suppression. |
| Cancellation races a review event | `test_restart_replays_accepted_review_racing_waiter_cancellation` starts concurrent reconciliation and cancellation against an already committed acceptance. The cancelled waiter has no execution or call, even when the wake is replayed again. |
| Revoked role and old wait | `test_wait_admission_and_wake_recheck_role_grant` and `test_cycle_scope_cancel_and_revoked_grant` check the live grant and old epoch. |
| Cross-work dependency and cycles | `test_cycle_scope_cancel_and_revoked_grant` covers scope denial and a cycle, including concurrent opposing registrations. Closure and OR/AND decisions are separately tested in `test_closed_prerequisites_never_synthesize_an_action`. |
| Lost provider reply; preventing resend | `test_unknown_call_recovery_pauses_until_reconciled`, `test_project_authority_freeze_and_unknown_fence` and `test_repeated_unsent_quota_wait_resumes_on_timer_after_restart` cover both ambiguous send and positively unsent/refunded outcomes. |
| Database temporarily unavailable | `test_idle_pool_retires_and_listener_recovers_sql_outage` injects an operational database error and checks reconnection without rerunning an owned call. Startup still requires an available database for binding authentication. |
| Redis temporarily unavailable | `tests/reliability/test_p55_eventhub.py` and the two-service recovery receipts cover publish failure, replay of missing positions, consumer death before ACK, duplicate delivery and stream reconstruction. |
| Multi-work, multi-organization fairness | `test_weighted_organizations_and_books_keep_their_share_after_restart` keeps four branches eligible. With organization and within-organization weights 1 and 2, 18 successful admissions split 2, 4, 4, 8 across the four works, including a mid-run supervisor reconstruction. This is a finite fairness test. |
| Backlog and resource bounds | The 500/1000/2000 workflow probes record actual checked-out connections, admitted futures, in-flight fixture calls, process RSS and elapsed times. A shared application process with two worker slots and an eight-connection pool serves the two books. |
| Platform shutdown | `test_native_host_reaps_child_after_host_exits` checks ownership of the child. `test_supervisor_drains_sent_attempt_and_leaves_next_job_ready` checks a controlled stop and restart. Abrupt termination uses lease and Attempt recovery. |
| New world state invalidates a suspended task's dependency | Both timings of `test_suspended_generation_keeps_semantic_read_fence` change a fact the task read. A conflicting input makes the task STALE. An unrelated world change can leave a fixed authorized read valid. |

Stdio, HTTP, PG LISTEN/NOTIFY, Redis and the in-process model fixture exercise different boundaries. Platform and suite-specific run receipts appear in the [platform audit](PLATFORM.md), [scheduler audit](SCHEDULER.md), [entrypoint review](entrypoints.md) and [Workshop receipts](raw/workshop/README.md).
