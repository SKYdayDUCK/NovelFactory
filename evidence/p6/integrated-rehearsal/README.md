# Combined checkout rehearsal

The code and templates from P6 PR #16 at 5800299aafdc4495ba031c3d139a32d0a98c3d70 and NF PR #11 at 74350760dfd063f3596e5b6838a56a40d3006ad6 merge without a conflict. The combined Git tree was 9b49e7594760680657ad2dc8bd797d8c27a9b908. A local detached worktree at that tree ran the two stdio services from the same checkout under independent database roles.

From the Workshop directory with the two-service variables in `workshop/benchmarks/README.md`, run `python -m pytest -q integration/test_dual_stdio.py integration/test_manager_resume.py benchmarks/cross_service_scene.py integration/test_paired_restore.py`. Five tests passed. The offline P6 input checks were run with `python scripts/P6_preflight_smoke.py --nf-checkout "$PWD"` in the NF root. The rehearsal did not send a production provider request or constitute a main-branch merge. Imported NF tests emitted marker-registration warnings when collected with Workshop's pytest settings.
