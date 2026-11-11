# W6 integration candidate

NovelFactory peer on GitHub: `becc8e7a400756c67832e0aca16cac78000df661`. The scene records contain the content fingerprints of both local source trees. Both services run as separate processes, on separate PostgreSQL roles and databases. The commercial model call count is zero.

With locked dependencies, set the variables in `workshop/benchmarks/README.md` and use the pinned Codex host executable on PATH. In the `workshop` directory run:

```sh
aw-migrate upgrade
python -m pytest -q tests --junitxml=pg-redis.xml
python -m pytest -q integration/test_dual_stdio.py integration/test_manager_resume.py benchmarks/cross_service_scene.py --junitxml=dual-scenes.xml
AW_PAIRED_RESTORE_EVIDENCE="$AW_ACCEPTANCE_OUTPUT/paired.json" python -m pytest -q integration/test_paired_restore.py --junitxml=paired.xml
```

The installed PostgreSQL 17 binary distribution must include pg_basebackup and pg_verifybackup. `../recovery` relative to the W6 evidence directory contains the recovery receipts. For load measurements use the unchanged contracts and separate empty databases described in the benchmark README. Unit templates in workshop/adapters/systemd passed systemd-analyze verify after substituting the actual local virtualenv path for the deployment prefix. The exercise used temporary services without installing units on the workstation.
