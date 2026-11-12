# 逐分支审计索引

正式 main 前提为 `630c4bcf1f1a7d71f1632d6eb2d273923ac08014`。共审计 91 个远端分支。逐对象清单和依赖位置位于同目录下的 `branch-inventory.json.gz`。执行时的实际前后分支数、精确原 SHA 和删除回执以归档 release 对应运行记录为准。

| 分支与 tip | 相对 main 超前 / 落后 | 独有历史 blob | 类别 | 拟处理及理由 |
| --- | ---: | ---: | --- | --- |
| `audit/combined-qualification-0f80173` · `9362eeb4664bf360560eb4d3e8a130e290a61c88` | 5 / 159 | 17 | B | 恢复独有提交和对象后清理 |
| `audit/hosted-validation-archives` · `1352c609c399708f485881fa57df6cf26379c57d` | 14 / 195 | 40 | B | 恢复独有提交和对象后清理 |
| `audit/nf-bridge-after-verified-w1` · `4badb53a6804ea391ede0438689d977f9a4ed23b` | 0 / 171 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/nf-bridge-signed-w1-hosted` · `0da226d1aef0e2500d798ce81a8ae62c494efe3f` | 2 / 171 | 4 | B | 恢复独有提交和对象后清理 |
| `audit/nf-native-signed-install` · `e62e2b6c8de6da4e61d87eb2a8680861f0a6b30f` | 1 / 171 | 1 | B | 恢复独有提交和对象后清理 |
| `audit/p1-ci-verification` · `cf7454ff3699d1d33b7d2af40822c45ac18d6a79` | 1 / 290 | 1 | B | 恢复独有提交和对象后清理 |
| `audit/p6-final-peer-qualification` · `c9a10e725ec172250c0d57dd6ed944c4fb1f6539` | 5 / 248 | 12 | B | 恢复独有提交和对象后清理 |
| `audit/pr2-pr3-20261111` · `f891e08041938e4d06172d6d6b324296dfa4d0cc` | 1 / 269 | 318 | B | 恢复独有提交和对象后清理 |
| `audit/release-coverage-review` · `0aa4e673f5079563027330e01cb6784235ff63de` | 4 / 189 | 7 | B | 恢复独有提交和对象后清理 |
| `audit/release-integrity-gates` · `b0478367a97399566f88f1d008b8f73d94aa1fd9` | 8 / 248 | 20 | B | 恢复独有提交和对象后清理 |
| `audit/release-integrity-tests` · `9891832cd956cf58392dfbb474e0676b1b06893a` | 9 / 248 | 20 | B | 恢复独有提交和对象后清理 |
| `audit/serial-delivery-receipts` · `bf9b164c15c9954f5518ddd8847440cc2054161e` | 10 / 195 | 12 | B | 恢复独有提交和对象后清理 |
| `audit/ubuntu-tools-install` · `c0673d4dff7d09f067f7896db168d48847e88eec` | 3 / 197 | 4 | B | 恢复独有提交和对象后清理 |
| `audit/w3-upgrade-outbound-authority` · `af77308accb83822c3b8cbde5efda39e6efff3c4` | 0 / 259 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w5-upgrade-resume-authority` · `d308e1ce1986a33e4781be97fba8a862b89dd76f` | 0 / 255 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-all-engineering-gates` · `34e4e7658b8a6dba7055a6cab1c1a55971176bba` | 0 / 122 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-at43-at49` · `90f7e68cdb390a1ed1a6417a48afd7bda1d23808` | 0 / 238 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-at43-at49-evidence` · `5fa0010d3ce5db637706c6f0d96a8077fede9623` | 7 / 241 | 18 | B | 恢复独有提交和对象后清理 |
| `audit/w6-codex-role-independent-mcp` · `450e4415240eb1ea2dfbba4a4ca708b9fa6aa8e0` | 0 / 252 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-installed-roles-integrated` · `73050eab6c5b64b79e58d6d1db3931148cf61e83` | 0 / 118 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-installed-wheel-qualification` · `7d2ed46140e69d5c6ecd3745410e324f2adce8eb` | 1 / 249 | 1 | B | 恢复独有提交和对象后清理 |
| `audit/w6-integrated-stage-qualification` · `9ada7a1b9fb13eed23931b24b28ccb4f272bb560` | 0 / 119 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-matched-admission-and-four-presets` · `ed032ec2ee87cea3122c6e95ee8b3d866ebc278d` | 0 / 243 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-owned-capacity` · `538a6750f8e17df8e4b408ce6589d741dfe671d4` | 11 / 243 | 26 | B | 恢复独有提交和对象后清理 |
| `audit/w6-owned-installation-qualification` · `c8589a72fa8e907955c8ea8c82dc1260a84bee19` | 11 / 243 | 28 | B | 恢复独有提交和对象后清理 |
| `audit/w6-owner-live-gate` · `aea2dfba5a4296c95059b2a1f22e74698e158838` | 7 / 243 | 17 | B | 恢复独有提交和对象后清理 |
| `audit/w6-preset-slot-regression-check` · `08e5aaf85399e90c3e1241b8fcc5873cdc8117da` | 9 / 243 | 23 | B | 恢复独有提交和对象后清理 |
| `audit/w6-qualification-after-owned-rosters` · `d8dccc04c46cca7c9138431bf038638156f87f89` | 0 / 229 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/w6-routed-actor-and-running-loss` · `b29c33d01609d7af3bcc7b86247117ab5ea8328f` | 12 / 243 | 29 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-main-verification` · `c6e5e8d443506fc2333d5c13b758ef8b5c802947` | 17 / 195 | 48 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-preview-schema-recovery` · `cfd4c8d2ffffdb80ee01c5754a9124a05c20c0b9` | 1 / 175 | 4 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-serial-qualification` · `e52e0b45f990d6b4c525eb17ce5f06ceb5149a98` | 10 / 251 | 87 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w1-preflight` · `3b56ae965ac8bbd64bbbfe17b32d0d3ddb4c5885` | 1 / 189 | 2 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w2-ownership-preflight` · `fad83f3506afca505a9de9c7c449b66feac5d441` | 2 / 181 | 11 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w2-preflight` · `22dc51ea2a08316834c15fa28108bdcc48bc2b02` | 1 / 189 | 3 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w2-routing-exercise` · `39f0acfbac2a4dd080bfee10c47141b127540581` | 4 / 178 | 7 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w2-routing-matrix` · `de46f67744149efb210629acf27abcf9aef220b0` | 3 / 178 | 8 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w2-with-installation-ownership` · `47dce9a8de4c1e31a79e4f6a774bcc847062269e` | 0 / 178 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/workshop-w3-canonical-dual-run` · `b89d0c17ecfb517b6dfa23e73f701474a80dc95f` | 4 / 161 | 8 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w3-canonical-w2` · `d8c4e9707a76f35e9f9dfdaca929c1767c650a02` | 2 / 169 | 6 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w3-ownership-preflight` · `39b9a1ba9a7a2724915b127c7742d1af5c462a56` | 2 / 178 | 8 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w3-with-installation-ownership` · `7b04454e1514447fb8e7ee4bb5c99e06f10734c5` | 0 / 174 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/workshop-w4-legacy-receipts` · `42db558333d88e639107e2616201ff545619297f` | 8 / 168 | 17 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w4-legacy-review` · `41a27254c8f6f1fcbeeb79a643034642d0e4aae3` | 1 / 258 | 1 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w4-ownership-preflight` · `dda7a82145474fd7f5a401b7693127263b915fd8` | 2 / 174 | 6 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w4-review-qualification` · `4f7d8ca35bdf0e04045986950edf5819bd572e80` | 0 / 165 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/workshop-w4-with-installation-ownership` · `9905f5373b0b6b1f6afd86a5dd4f70d0cd7c0a08` | 0 / 171 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `audit/workshop-w5-ownership-preflight` · `bc6fbbe71078b825872d313d5ef4f2d95aa2ddcb` | 2 / 171 | 11 | B | 恢复独有提交和对象后清理 |
| `audit/workshop-w5-with-installation-ownership` · `bfb74f681435b12a385eb54e596c658e64629276` | 0 / 167 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `ci/archive-at30-s9-delegation` · `e74d0156e22b62a9632603c726762dceeab822a9` | 1 / 197 | 2 | B | 恢复独有提交和对象后清理 |
| `ci/at30-delegation-pair` · `143c33c5c6a9d87624a0ada36228956322d7db75` | 1 / 197 | 1 | B | 恢复独有提交和对象后清理 |
| `ci/p6-shared-budget-pitr` · `e997300e361d5067392ee3cc25873f765cb8d3c6` | 1 / 189 | 2 | B | 恢复独有提交和对象后清理 |
| `ci/workshop-codex-dual-ssh` · `4ae3ff359d471481528ff42dc15058a16eb3b834` | 3 / 254 | 5 | B | 恢复独有提交和对象后清理 |
| `ci/workshop-queue-auth-upgrade` · `e3dd6274fe056f1537f34f66b1e6e26af20e6d78` | 2 / 256 | 4 | B | 恢复独有提交和对象后清理 |
| `dev/aw-w0` · `e5a47f51f39e3ad618e822e451eb89110395e7dc` | 0 / 190 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/aw-w1` · `48c20a10c1e28af608a4c52f207351eaa1c45e06` | 0 / 181 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/aw-w2` · `162af2695b76b20c0257b28217a3eb5b4096d7f6` | 0 / 175 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/aw-w3` · `a15f41b40ed4a0cd4716e7335954ec0a1c5efadf` | 0 / 157 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/aw-w4` · `1cc12cab018213e05b70c226dcdf63974ee7dd93` | 0 / 137 | 0 | C | active Actions run #43096270977；active Actions run #43096271063；active Actions run #43096147056；active Actions run #43096147090；active Actions run #43095717290；active Actions run #43095717395；active Actions run #43095475603；active Actions run #43095475584 |
| `dev/aw-w5` · `455cf92cc6fe5e1d0e565d627a4d5651a56bc8d4` | 0 / 119 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/aw-w6` · `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771` | 0 / 71 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/nf-workshop-delegation` · `695668b87f6a76acbe7e27c71fcb259b7b3f0795` | 0 / 163 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/p2-production-loop` · `a9547bd651e177d48c9d0e810362e411f510d62d` | 0 / 277 | 0 | C | operational reference: .github/workflows/p2-load.yml；operational reference: .github/workflows/p3-p4-evidence.yml；operational reference: .github/workflows/p1-integrity.yml |
| `dev/p3-p4-continuity` · `23a61398278c9a13e6450d444d4a6d25a88ef5e0` | 0 / 269 | 0 | C | 开放 Scion PR #4 的原 base；open PR #4 base；operational reference: .github/workflows/p1-integrity.yml；operational reference: .github/workflows/p2-load.yml；operational reference: .github/workflows/p3-p4-evidence.yml；operational reference: .github/workflows/p3-scale-experiments.yml；operational reference: .github/workflows/p4-recovery-drill.yml |
| `dev/p5-5-redis-eventhub` · `67e8836fd8aceb5d4b8d054594f5f3bdfdddaac9` | 0 / 196 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/p5-agent-runtime` · `f2e6d38534214e79630b807cec9a720d99e975b1` | 0 / 229 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/p57-creative-harness-s1-s6` · `ece71f73292780211705114453ebfde1728195c5` | 36 / 23 | 89 | C | P5.7 主线签收和独立审查原件；operational reference: reports/code_review/p57_creative_harness/MAIN_MERGE_RECEIPT.md；operational reference: reports/integration/CURRENT_STAGE.md；operational reference: reports/integration/NEXT_AGENT_HANDOFF.md；operational reference: reports/p6_readiness/P58_BASELINE.md；operational reference: .github/workflows/creative-harness.yml |
| `dev/p57-s0-architecture` · `a8073b6664df8953330dd3bc0240038fcb64f09a` | 0 / 44 | 0 | C | operational reference: .github/workflows/creative-harness.yml |
| `dev/p58-agent-runtime` · `49ee0e7c83d3b01b974d29137c1757da8b7c2b4b` | 2 / 0 | 18 | C | P5.8 开发和分阶段签收；active Actions run #43227148018；active Actions run #43227148324；active Actions run #43227148125；active Actions run #43227148080；open PR #30 head |
| `dev/p6-local-validation` · `6c01eee41f0366dd9a136038161b2d030d07ab67` | 0 / 52 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `dev/p6-token-ceiling` · `f96d26bcf449d8e7905099402ddb4ddcf8679a60` | 0 / 62 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `evidence/at30-s9-delegation-abba` · `373cb63305d2066013e2e4a6b42382d801bd189f` | 1 / 197 | 6 | B | 恢复独有提交和对象后清理 |
| `evidence/nf-bridge-p6-final` · `f0c62698c6dbc3cb6acc0be025b7e359f600ad88` | 4 / 189 | 8 | B | 恢复独有提交和对象后清理 |
| `evidence/p55-55d897-guard` · `3985ebd4f90ff9f7d2e9626df9c0fbf5e8cdb1be` | 9 / 204 | 17 | B | 恢复独有提交和对象后清理 |
| `evidence/p55-http-query-comparison` · `fb5622d0a0b7c6baef7721457399a1a63cd3a0b3` | 4 / 203 | 9 | B | 恢复独有提交和对象后清理 |
| `evidence/p55-receipts-55d897` · `4be08f03512bc20040f517306532a26a32348dc5` | 24 / 204 | 180 | B | 恢复独有提交和对象后清理 |
| `evidence/p55-s9-business-compare` · `695e048ea936f6eebf5c223747797aaece5bb757` | 3 / 204 | 10 | B | 恢复独有提交和对象后清理 |
| `evidence/serial-delivery` · `7d7c4864d85c5049a6604bf52f14bc39fc356a77` | 153 / 190 | 870 | C | operational reference: reports/integration/P56_SYSTEM_ACCEPTANCE.md；operational reference: reports/integration/PRE_HARNESS_BASELINE.md |
| `evidence/workshop-codex-dual-ssh` · `ed4ab4c18d365404b669f84b08b79bf4059ed156` | 4 / 254 | 8 | B | 恢复独有提交和对象后清理 |
| `evidence/workshop-queue-authority` · `c5f1241e5909c8e5f5ceecdeab2c1ca2932be0e7` | 3 / 256 | 8 | B | 恢复独有提交和对象后清理 |
| `experiment/p55-large-projection-witness` · `8f245a51729308fc892396d3a10d70a7ef1d7853` | 0 / 202 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `experiment/p55-query-shapes` · `598f4ee65fbb6f0ed9872656de965f070965b1da` | 1 / 204 | 2 | B | 恢复独有提交和对象后清理 |
| `experiment/scion-incremental-20261111` · `4f249d520c921c8d6effee9978f7338b912f1dd0` | 13 / 269 | 308 | C | 开放 Scion PR #4 的原 head 和旧基线证据；open PR #4 head |
| `experiment/scion-p58-compat` · `75276a791294d4b17035aead4e33352e865e01aa` | 1 / 0 | 15 | C | 已迁移至 P5.8 基线的独立 Scion 实验 |
| `integration/p56-full-acceptance` · `e5f5348ca07dc50c9fe52ab271a733ac31fa063c` | 0 / 66 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `integration/pre-harness-baseline` · `5aa9b71b66c7800f58214fa368e22ebeb9c3d56b` | 0 / 49 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `integration/w6-signed-98a8bb` · `e6f79f1063fd52acbaefd20dedd5a1f186b3168b` | 0 / 68 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `main` · `630c4bcf1f1a7d71f1632d6eb2d273923ac08014` | 0 / 0 | 0 | C | 正式主线；active Actions run #43226668635；active Actions run #43226668608；active Actions run #43226668528；active Actions run #43226668984；open PR #30 base |
| `perf/p55-http-query-validation` · `df30c306057d26b7b32e3cec0df1ad9926a6824a` | 0 / 203 | 0 | A | main 已包含全部历史，恢复通过后清理 |
| `recovery/predecessor-20261111T153406Z` · `06e533a2cb79eaafde74d46e2e74329f5b0d6991` | 1 / 198 | 1 | B | 恢复独有提交和对象后清理 |
| `recovery/scion-s5-evidence-20261111` · `c019d971656426a5680d95d2c1bc5a35154d42fa` | 14 / 269 | 312 | B | 恢复独有提交和对象后清理 |
