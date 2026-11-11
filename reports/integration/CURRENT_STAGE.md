# 当前唯一阶段

Workshop W4 [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) → `main`。冻结候选 `6f07f8eb8e29187bea24e0c90ce7798996dcfc42`，树 `08ff91ac91aa3f9bbb0295156a18d2db5cb11895`。正式基线为 W3 `e6763d844f023d7c6a13060dbaebca0416fe3099`。已补真实旧库升级、多主管下查询/复核和原键重试的团队边界。[旧版越权复现](evidence/W4/preflight/team-before.xml) · [修后独立 PostgreSQL/stdio](evidence/W4/preflight/team-after.xml)。

当前运行本候选的双 PG17/Redis8/stdio/PITR、本地 Linux 角色、[正式 Workshop CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096147056) 和 [工程整体 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096147090)。核验本 SHA 后正常合并，再测试合并后的主线、保存签收。W5 冻结。
