# 多用户账号、会话与数据权限

运行以下命令初始化管理员。新安装会提示输入密码，不把密码写入 `.env`；同时生成稳定的 `CHAINLIT_AUTH_SECRET`。恢复码只显示一次，请保存到密码管理器。

```powershell
.venv\Scripts\python.exe scripts/setup_history.py
.venv\Scripts\data-agent-ui.exe
```

打开 `http://localhost:8000` 登录；右下角“注册 / 恢复账号 / 账号管理”链接进入 `/account`。新用户可注册，默认没有业务数据源访问权限。注册关闭时配置 `DATA_AGENT_ALLOW_REGISTRATION=false`。

管理员在应用服务器运行以下命令分配权限，配置名称对应 `src/data_agent/config/*.yaml`。授权是替换当前权限清单；不传 `--profiles` 会移除全部授权。授权变更后用户须重新登录。

```powershell
.venv\Scripts\python.exe scripts/manage_users.py grant alice --profiles contoso
.venv\Scripts\python.exe scripts/manage_users.py create bob --profiles amex
.venv\Scripts\python.exe scripts/manage_users.py revoke alice
.venv\Scripts\python.exe scripts/manage_users.py disable alice
.venv\Scripts\python.exe scripts/manage_users.py enable alice
.venv\Scripts\python.exe scripts/manage_users.py reset alice
```

管理命令只供拥有服务器文件访问权限的管理员使用，没有公开的用户授权 API。`create` 与 `reset` 交互输入密码，避免密码出现在命令行参数中。恢复码丢失时，管理员核实身份后用 `reset` 设置新密码并安全交付新的恢复码；此操作撤销旧登录，但不自动启用已禁用账号。

## 密码与恢复

账号独立保存在 `local_history/accounts.sqlite`，每个账号具有稳定 UUID、随机盐及 scrypt 密码哈希。新密码长度为 12–128 字符；恢复码具有随机熵，仅保存 SHA-256 摘要。用户名为 3–64 位 ASCII 字母、数字、下划线或短横线，区分大小写。

恢复需要用户名、一次性恢复码及新密码。更新在 SQLite 事务内完成，恢复码不能被并发重复使用。成功后返回新的恢复码，撤销所有旧登录。登录后也可在 `/account` 使用当前密码修改密码并轮换恢复码；失败的密码修改不影响现有恢复码。如果无法登录且恢复码丢失，目前须通过受信任管理员另行处理，系统不提供邮件发送或邮箱验证。

已有 `.env` 的 `DATA_AGENT_LOGIN_USER/PASSWORD` 在首次启动时导入账号库，保留用户名和原来的历史记录归属。导入只发生一次，修改 `.env` 不会覆盖已存在的密码或授权。确认登录后清空旧环境密码，并在 `/account` 修改密码取得新的恢复码。已经存在账号时，重复运行初始化脚本不会重置密码。

## 会话和历史隔离

Chainlit 签名登录有效期为 8 小时。账号 UUID 与认证版本随登录签名保存。服务端验证账号状态及版本；恢复、改密、退出、退出所有设备、禁用和授权变更都会使旧版本失效。当前“退出”同时撤销该账号的其他设备登录。已连接 WebSocket 在下一个应用事件时拒绝失效登录；撤销不会中断已经开始执行的一次查询。

历史列表、内容、反馈、附件及修改/删除操作验证当前账号归属，记录不能通过重复使用 ID 转移到其他会话。恢复会话先检查作者，再打开 Agent 状态。图检查点使用 `历史用户 UUID:会话 ID` 命名空间；旧账号恢复已有对话时，仅迁移该账号拥有的旧检查点，保留待确认计划。附件接口要求有效登录和会话归属。会话分享保持关闭。

账号权限限定可加载的 YAML 配置，只有这些配置中的数据助手会建立连接、进入规划目录；Harness 同时使用其助手白名单，且与全局 Harness 白名单取交集。客户端指定其他配置会在建立连接之前被拒绝。

这里隔离的是个人对话、附件、Agent 状态及数据源访问权限。同一个业务配置被授权给多个账号时，这些账号仍可查询同一业务数据库中的行。若需要租户行级隔离，应另配租户数据库或数据库 RLS 与租户身份连接；本实现不会把一个共享连接字符串自动变成租户数据库。CLI/A2A 仍是受信任操作入口，尚未接入这些网页用户账号，不应直接作为公开多用户接口。

## 部署和验证

登录、注册和恢复请求有单进程 IP 速率限制；已有账号的连续 5 次密码失败锁定 5 分钟，恢复成功清除锁定。变更接口拒绝跨站 Origin/Fetch 请求，账号页面禁止嵌入且返回 no-store。默认只允许 localhost 与 127.0.0.1 的 WebSocket 来源；部署域名须同步修改 `.chainlit/config.toml` 的 `allow_origins`。公开部署使用 HTTPS 和正确代理来源配置；入口代理承担跨进程的统一限流。用户不能读取服务器本地账号库、数据库凭据或运维日志。

备份 `accounts.sqlite`、`chats.sqlite`、`checkpoints.sqlite` 和签名密钥。运行中的 SQLite 数据库需通过备份接口或停服务后复制，避免遗漏 WAL 文件。默认实现适合单应用实例；多个应用实例的 WebSocket 会话与限流需另接共享存储。

离线测试使用真实 SQLite、HTTP 与 JWT，覆盖注册、哈希存储、并发恢复、密码变更、失效会话、越权读写、数据源权限和旧会话恢复，不调用模型服务：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_accounts.py tests/test_history.py -o addopts='' -p no:cacheprovider -q
```
