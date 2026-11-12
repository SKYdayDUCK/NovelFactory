#!/usr/bin/env bash
set -eu
: "${P59_OUTPUT:?P59_OUTPUT must be a new evidence directory}"
mkdir -p "$P59_OUTPUT"
python -m pytest -q -p p59_meter --junitxml "$P59_OUTPUT/junit.xml" \
 tests/queries/test_history_queries.py \
 tests/reliability/test_p55_eventhub.py::test_multiple_relays_competing_consumers_and_independent_subscriptions \
 tests/reliability/test_p55_eventhub.py::test_relay_crash_before_recording_and_worker_crash_before_ack \
 tests/integration/test_workshop_delegation.py::test_stdio_receipt_survives_lost_reply_and_supervisor_execution \
 tests/integration/test_workshop_delegation.py::test_atomic_rejection_concurrent_replay_and_revocation \
 tests/integration/test_harness_planning_mcp.py::test_separate_planner_and_controller_stdio_ports \
 tests/integration/test_p58_routing.py \
 tests/integration/test_p58_production_pipeline.py::test_native_parallel_drafts_conditional_plans_and_early_review \
 tests/integration/test_harness_review_workflow.py::test_directed_revision_and_independent_clean_reread \
 tests/integration/test_p55_statistics.py::test_paused_worker_catches_up_then_rebuilds_without_redis \
 tests/integration/test_p5_supervisor.py::test_process_death_after_provider_received_request
