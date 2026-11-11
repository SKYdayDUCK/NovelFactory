# W4 skill and review acceptance

Run from workshop with an isolated Workshop PostgreSQL 17 database.

```sh
alembic upgrade w004
python -m pytest tests/test_reviews_skills.py tests/test_delegation.py -q
```

Seventeen tests passed on PostgreSQL 17.11. Fifteen W4 cases cover the five skill access policies, live revocation with immutable task hashes, seven review modes, shared producer/proxy subjects, two-round escalation, bounded evidence, hard budgets, direct review slot denial and UNKNOWN holds. The other two test W3 reply loss and pre-send revocation. A test setup initially requested a token ceiling different from the trusted NF slot. Matching the fixture to that frozen ceiling produced the reported passing result.

The review intake executes deterministic arithmetic checks on operator-verified observations. Only numeric counts, opaque source receipts, SHA-256 digests, subject group IDs and scoped evidence references are retained. The operator imports provenance using `python -m agent_workshop.review_admin subject|skill|artifact|review-slot`. The operator must verify the source and all producer/coauthor/proxy mappings before import. This interface does not independently authenticate a provider's export. No model endpoint was called. The paid-review case exercises actual Workshop budget transactions with seeded NF receipt states. Genuine two-service receipts, artistic performance and provider invoice accuracy are separate gates.
