# W2 independent main replay

Source and ending checkout `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`. The replay used Linux, Python 3.12, two PostgreSQL 17.11 clusters with distinct service accounts, Redis 8.0.2 and the pinned Codex CLI. No external model calls.

| Check | Local result |
|---|---|
| Workshop suite | 34 passed, 1 skipped because the Docker sidecar was unavailable |
| Previously deployed databases | 3 passed |
| NF scoped tool matrix | 5 passed, nine tools discovered, invoked and denied where required |
| Two MCP servers and independent SQL identities | 2 passed |
| Additional Redis process stop and restart | 1 passed using native redis-cli/redis-server. The shell harness in the archive shows the two process-control substitutions. |
| Root unit, contract, evaluation and admission suite | 398 passed |
| Design package and original mechanism tests | package passed, 35 mechanism tests passed |

The archive includes original JUnit files, runtime matrices and the takeover branch/PR snapshot. SHA-256 `584b2f5c3912826316c5c20b2a3bc1ff27094bbe68eaaf8b7947aeb9f86d3ac4`. The separate [GitHub Workshop job](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43076279221) ran the original Docker sidecar test.
