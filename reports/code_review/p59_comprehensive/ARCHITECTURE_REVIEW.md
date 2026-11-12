# Authority and service boundaries

At commit `5e322e8af8ddb4465f68f8cab84c9685832461a9`, the NF application service accepts world transitions. Worker and model outputs are candidates. Workshop owns organizations, signed delegations and skill routes in its own database. A delegated job acquires an NF task binding before the runtime can spend an NF reservation. The outbound call can outlive its caller, so its durable Attempt owns uncertainty. PG fencing and reconciliation sit on both sides of the provider boundary.

| Flow sampled | Authority traced | Independent evidence |
|---|---|---|
| Author resume → supervisor → role | ProjectBinding in mcp_runtime.py; live task snapshot/role-tool intersection in available_tools | tests/integration plus Workshop dual stdio |
| Scene plan → role → candidate → decision | creative_context_store.py; creative_review.py; kernel.py formal acceptance | S1–S6 tests, including superseded input and concurrent adoption |
| Workshop → NF → host | Delegations, adopted signed policy, scoped route, private credential resolver | [Workshop](WORKSHOP_REVIEW.md), [Runtime](AGENT_RUNTIME_REVIEW.md) |
| Parallel chapters → new volume | production_pipeline.py stores lineage and records; NF Attempts hold actual usage | [Pipeline](PIPELINE_REVIEW.md) |

The tests trace individual boundaries. They do not establish production throughput, real commercial settlement, or a human quality rating. The checkout and evidence branches were read separately. The Runtime/Workshop reviewer worked in a separate agent context; the core reviewer also prepared its proposed repair. Both use the same model family.
