# Evidence snapshot

This frozen snapshot includes closed scenario receipts from the original 50/200 mixed and overload runs, 500 mixed, two independent 500-agent 600-second read soaks, 1000 open, 10 mixed and isolated candidate runs. The primary collector was still repeating scenarios already covered by the completed replica at the cut. Its in-flight log, unfinished data and outer-session receipt are excluded from this snapshot. Snapshot metadata and the final archive are prepared independently of running processes.

The scope candidate is tied to the exact source digest in environment.json. The losing validator experiment is in VALIDATION_EXPERIMENT.md; its unguarded ownership counterexample, validated early/late rejections, and every timing sample are in raw/validation-pair.json. The earlier infrastructure and harness failures are listed in REGRESSION.md.
