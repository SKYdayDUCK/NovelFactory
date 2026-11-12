# W1 organization acceptance

PostgreSQL 17.11, Python 3.12, w000 → w001 in an isolated Workshop database with a separate owner. The underlying cluster used fsync, synchronous commits and checksums. Run from workshop with AW_DATABASE_URL pointing to an isolated Workshop test database.

```sh
alembic upgrade w001
python -m pytest tests/test_organization.py -q --junitxml=w1-junit.xml
```

Seven cases passed. They cover all four presets, repeat bootstrap, manager-only CAS changes, simultaneous hires with matching and differing keys, historical employment, departure/reactivation fencing, self-supervision denial, and the actor's limited directory. The bootstrap code issues SQL only. No model provider was configured or invoked. This is PostgreSQL application evidence. Native-host MCP, remote CI and full end-to-end creation have separate gates.
