# Test and evidence coverage

The exact frozen main passed both jobs in GitHub integrity run 43228784812. This is a baseline receipt. A separate local PG17 run in raw/p58-focused.xml ran 33 selected runtime, provider and production tests; the upstream model responses were synthetic. A real local Codex process and an actual commercial inference request are different units of evidence.

The targeted timeline test used actual migrated databases through scripts/with_postgres.py. Four failing cases and four control cases are preserved before repair. A second hypothesis about PLAN_DEPENDENTS and stale search was discarded because the request schema rejects that combination. raw/rejected-hypothesis.log preserves the observation; it is excluded from the findings count.

The full single- and dual-service suites are tracked in the later engineering regression record. No production deployment, commercial charge, real-reader panel or live deployment recovery objective has been tested by these probes.
