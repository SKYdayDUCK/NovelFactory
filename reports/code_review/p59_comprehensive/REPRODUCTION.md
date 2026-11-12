# Reproduce the frozen observations

Check out 5e322e8af8ddb4465f68f8cab84c9685832461a9. Install the core and workshop locked dependencies using Python 3.12. Supply PG_BIN for PostgreSQL 17.11 and use a disposable directory. The query test uses package-relative fixtures, so put raw/test_time_axis.py under tests/queries first.

```bash
python scripts/with_postgres.py -- python -m pytest tests/queries/test_time_axis.py -q
python scripts/with_postgres.py -- python -m pytest tests/integration/test_p58_production_pipeline.py -q
PYTHONPATH=. python reports/code_review/p59_comprehensive/raw/catalog_check.py
```

The pipeline ledger dump requires NF_P58_PIPELINE_TEST_EVIDENCE pointing to an output JSON file. Use separate DBs for concurrently running suites. [IDs, expected results and trigger conditions](FINDINGS.json).
