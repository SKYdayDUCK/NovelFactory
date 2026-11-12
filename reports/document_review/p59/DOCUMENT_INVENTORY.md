# Public documentation inventory

The two landing pages lead to ten current guides per language. Legacy designs, specifications and acceptance records keep their old paths and historical links. The current design and API claims are sourced from the checkout. File hashes of both the draft and final versions are in the independent editorial evidence.

| Chinese | English |
| --- | --- |
| README.md | README.en.md |
| docs/zh-CN/01-项目介绍.md | docs/en/01-project-overview.md |
| docs/zh-CN/02-快速开始.md | docs/en/02-quick-start.md |
| docs/zh-CN/03-系统架构.md | docs/en/03-system-architecture.md |
| docs/zh-CN/04-Agent系统.md | docs/en/04-agent-platform.md |
| docs/zh-CN/05-Agent-Runtime.md | docs/en/05-agent-runtime.md |
| docs/zh-CN/06-小说生产流水线.md | docs/en/06-production-pipeline.md |
| docs/zh-CN/07-接口参考.md | docs/en/07-api-reference.md |
| docs/zh-CN/08-配置与部署.md | docs/en/08-configuration-and-deployment.md |
| docs/zh-CN/09-运维与恢复.md | docs/en/09-operations-and-recovery.md |
| docs/zh-CN/10-性能与实验.md | docs/en/10-performance-and-evaluation.md |

The shared reference contains docs/reference/catalog.json and examples.json. The first enumerates registered inputs and their definitions; the second gives one test-backed invocation per registered interface, six path-binding examples and the test from which each response came. scripts/export_public_api.py compares live registration, validates examples and checks test references. The public CI runs this check.
