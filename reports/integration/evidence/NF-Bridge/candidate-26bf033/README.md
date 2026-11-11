# NF Bridge candidate verification

PR #11 targets W2 main `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`. The candidate `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` has tree `28dcb1d0ef5ce4e05241318aafd9c52fb2824267`. The independent Workshop test peer remains pinned to `ac579e0b89b55e613d32750ce1c07210e0b07022`.

[Paired database replay](paired-pg17.zip) contains the exact checkouts, three JUnit files, commands and the WAL restore receipt. On PostgreSQL 17.11 the seven NF integration tests, live dual MCP lost-reply test and physical recovery test all passed without skips. Both replay LSNs matched their recovery targets. Recovery produced one NF task, one synthetic loopback request and zero external provider requests. The ZIP SHA-256 is `53fcdbe246336dff4acf2fa97fdcbc0e161032b1e8837f1aaa2793a07478231b`.

[Expiry regression controls](expiry-control.zip) preserve the two failing controls, two repaired controls and twelve passing bridge/supervisor tests on isolated PostgreSQL databases. The same pre-network transport probe identified both attempts to initiate new work under an expired installation. Existing UNKNOWN invoices remain reconcilable. This test set used local code commits recorded in its manifest. The resulting changed source and tests are present on the GitHub candidate.

[Root integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135131) · [Physical restore](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135238) · [P2 capacity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135306) · [P3/P4 evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135154) · [Scale](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43079135239)
