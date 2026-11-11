# Codex 的双服务角色配置

角色的 NovelFactory 任务租约限定作品、分支、角色、任务和代际。Workshop 使用独立签发的员工绑定。将同目录 `role-dual.example.toml` 的两段合入角色专用的 Codex 配置。服务端仍逐次验证活动租约或员工任期。Workshop 邮箱和名册用于组织协作，写入角色认知必须经过 NovelFactory 原有的世界与认知接受流程。

在两个受信服务账户下分别配置角色的专用 SSH 公钥。使用已核实的主机指纹、固定别名、独立密钥及独立数据库账户。私有绑定、数据库连接和 NF 对象目录仅允许对应服务账户访问。角色只需身份专用的 SSH 签名器、对应公钥和已核实的 `known_hosts`。OpenSSH 的 `IdentityAgent` 可指向该角色的签名器套接字，`IdentityFile` 可指定其公钥，同时配置 `IdentitiesOnly yes`。同一主机部署时两个别名也应指向不同的服务账户。

为 `nf-role-service` 在 NF 服务账户登记下列强制命令，使用管理员签发的角色任务绑定。为 `aw-role-service` 在 Workshop 服务账户登记第二条，用该员工当前任期的绑定。部署时替换实际受信路径和各自的公钥。

```text
restrict,command="/srv/novelfactory/.venv/bin/python -m novel_factory.interfaces.mcp_stdio --character-only --binding /var/lib/novelfactory/private/role.json" ssh-ed25519 <NF角色公钥>
restrict,command="/srv/agent-workshop/.venv/bin/aw-mcp --binding /var/lib/agent-workshop/private/employee.json" ssh-ed25519 <Workshop员工公钥>
```

在同一线程调用 `nf_task_get({})` 核对任务和代际，再调用 `aw_roster_get({})` 核对该员工可见的目录。提供的客户端工具清单仅缩小发现范围。更换角色任务、撤销 NF 授权或更改员工任期时，两个服务分别执行自己的授权校验。

作者继续项目使用 `config.example.toml` 的 `aw-author-service`，其强制命令选择作者或管理者的独立 Workshop 绑定。保留仓库 `adapters/codex/p5-mcp.toml` 中 `nf-resume-service` 的作者配置，并沿用本目录 `AGENTS.md` 所述的一次幂等续跑请求。

`workshop/acceptance/dual_codex/probe.py` 使用原生 Codex app-server 创建一个临时只读线程，核验两个服务器的发现与实际只读回复。隔离验收使用回环 OpenSSH 的强制命令、两个服务 OS 账户、两组独立 PostgreSQL 凭据以及真实角色任务。生产部署仍需核对操作系统权限、远端主机指纹和服务端授权。

在专用临时 Linux runner 上，安装 Git、uv、Python 3.12、OpenSSH 客户端与服务器，并提供原生 `codex-cli 0.167.0-alpha.4`。将 `AW_DUAL_TEST_ADMIN_URL` 指向可创建临时数据库与登录角色的测试用 PG17。验收脚本需要非交互 sudo 创建短期 POSIX 用户及回环 sshd。它使用随机账户、随机端口、两个空数据库以及临时 SSH 密钥，在正常退出或步骤失败后清理。工件目录仅写入 `receipt.json`。脚本从两个检出当前的确切 Git 对象导出代码，将 SHA 与线程、授权读取一起记录。

```bash
bash workshop/acceptance/dual_codex/run.sh \
  "$NF_CHECKOUT" "$WORKSHOP_CHECKOUT/workshop" "$CODEX_BINARY" "$ARTIFACT_DIR"
```
