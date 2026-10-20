# Monitor

Monitor 是可独立运行的云主机电源控制器和 QQ Bot。它通过 `power-api` 查询、开启或关闭
Manager 云主机，并通过受限的 Manager API 查询整合包状态；不使用 SSH、TAT，不执行
`manager.sh`，也不挂载 Manager 的 Docker Socket。

## 部署与命令

```bash
cp config.yml.example config.yml
cp .env.example .env
./monitor.sh check
./monitor.sh install
./monitor.sh status
./monitor.sh logs --follow
./monitor.sh stop
./monitor.sh start
./monitor.sh reset-targets --force   # 清空 QQ 管理员与订阅目标
./monitor.sh reset-messages --force  # 备份自定义文案并恢复默认模板
```

`manager.api` 指向 Manager 的 TCP API，Monitor 始终通过 HTTPS 使用 `MANAGER_API_KEY`
查询整合包、发送公开开机通知，并在关机前调用 `stop-all`。`host` 必须与 Manager TLS
证书的域名/IP 一致，通常与 Terminal 根配置的 `manager.host` 相同。根配置的 `monitor`
段保持完整、自包含，允许与 `manager` 段重复这些值，方便整段复制到独立部署的
`monitor/config.yml`。若 Manager 使用部署时生成的私有 CA，`data/tls/ca.crt` 会由
Terminal 自动上传。

`check` 只验证 Docker Engine 与 Compose 插件是否可用，不会安装或修改系统软件。
Monitor 不负责准备 Docker 系统环境；Terminal 部署时使用
`./terminal/mcsp.sh deploy monitor --install-docker`，手工独立部署则需先自行安装并授权 Docker。
`install` 会比较 `Dockerfile`、`.dockerignore` 与 `src/` 的内容哈希和
`data/image-source-hash`：源码未变且镜像存在时复用镜像，只执行
`docker compose up -d --no-build`；源码变化或镜像缺失时才构建，并在启动后清理本项目
旧的悬空镜像。`check` 只检查环境；`install --recreate` 强制重建容器，
Terminal 在 config.yml 或 .env 内容变化时会自动传入该参数。`add-admin`、`test-alert`
也复用当前镜像，不再每次 `--build`。

日常启停使用 `start`、`stop`，它们只操作 Monitor 容器，不关闭这台 7x24h 主机。

QQ 管理员与订阅目标由 Monitor 在运行时维护，不再写入 `config.yml`。管理员通过顶层终端添加：

```bash
./terminal/mcsp.sh monitor add-admin          # 生成验证码并等待私聊验证
./terminal/mcsp.sh monitor test-alert         # 向已登记的目标发送安全测试告警
```

`add-admin` 会临时停掉 Monitor 容器，等待你**私聊**机器人发送四位验证码；验证通过后
把管理员写入 `data/qqbot.json`，再重启容器。重复执行可添加多个管理员。

单聊用户或群聊都可以申请订阅告警：发送 `订阅 [昵称]`，昵称可以省略。省略时单聊使用
QQ 昵称（取不到则生成用户 ID），群聊使用生成的群 ID。机器人会把申请私聊发给所有
管理员；任意管理员在私聊中发送 `批准 <编号>` 或 `拒绝 <编号>`（也可以点卡片按钮）
即可处理。

- 已批准的单聊用户：接收异常告警，可发送“菜单”查看只读状态，不能操作电源。
- 已批准的群：接收服务器开机、关机等公开通知，可查看只读菜单；完成群聊身份绑定的
  Monitor 管理员可执行电源操作。
- 电源、定时开关机和告警静默只允许 admin。admin 在私聊和群聊中都可以操作。
- QQ 的 `union_openid` 可能为空。群成员需要 @机器人发送 `申请管理`，由现有管理员
  在私聊中批准后，Monitor 才会保存该群的 `group_openid + member_openid` 映射；
  群主和订阅申请人不会自动获得管理权限。
- 管理员可用 `状态` 查看已启用整合包的名称、版本与状态；发送 `日志`、`开机自启`
  或 `取消自启` 后，通过动态按钮选择整合包。日志默认返回最近 10 行。
- 异常告警和恢复通知只发送给管理员及已批准的单聊；公开开关机通知只发送到已批准群聊。
- 群订阅申请可以自带便于管理员识别的备注：`订阅 群备注`；省略时显示脱敏群标识。

## QQ 文案热加载

`src/message.yml` 是镜像内的默认文案。首次启动会复制到 `data/message.yml`，运行时读取
该文件；直接编辑宿主机的 `data/message.yml`，下一次消息或告警时会自动热加载。新文件
格式或服务 ID 无效时保留旧文案并在日志中告警，不会让 Bot 退出。

`deploy monitor` 不会覆盖 `data/message.yml`，改文案不需要重新部署或重启容器。
需要采用新版默认模板时执行 `./terminal/mcsp.sh monitor reset-messages`；远端会先为原文件
生成带时间戳的 `.bak` 备份，再原子替换并由运行中的 Bot 热加载。

`deploy monitor` 会保留 `data/`（包括 `qqbot.json`），不会自动清空管理员与订阅。
需要重新授权或清空全部目标时执行 `./terminal/mcsp.sh monitor reset-targets`（等价于远端
`./monitor.sh reset-targets --force`）。该命令会停止 Monitor，并由应用清空 `data/qqbot.json`，
再按原运行状态启动；之后需要重新执行 `add-admin`。

`auto-start/start-at` 和 `auto-stop/stop-at` 分别控制每日定时开、关机。内部调度器
每 30 秒检查一次；完成、取消的事件以及告警静默截止时间记录在 `data/state.json`，
API 临时失败会自动重试。首次创建状态文件时只记录当时最近的日程事件，不补执行
部署前已经错过的开关机；之后重启仍会重试尚未完成的当期事件。
重复关机以及 Manager 与 Monitor 同时关机按云 API 的幂等语义处理。

关机前 Monitor 必须通过 Manager API 调 `POST /v1/modpacks/stop-all`，等到任务成功后才
调用云厂商关机。定时关机和 QQ 人工关机都走这条路径；Manager API 不可达、密钥错误或
停止任务失败时，Monitor 会拒绝关机并告警，绝不直接关掉还跑着 Minecraft 的云主机。
`timezone` 使用 IANA 时区名称（例如 `Asia/Shanghai`），统一决定定时任务、消息时间和
`data/service.log` 时间；无效时区会在启动前被拒绝。

公开开机通知不会从 Monitor 配置读取静态整合包列表。Monitor 在 Manager 云主机进入
`RUNNING` 后使用受限调用身份调用 Manager API；只有接口确认至少一个整合包处于运行状态时，才向群聊
发送实际的整合包名称和 `host:port`。接口或游戏服尚未就绪时每 30 秒重试，不会
把“云主机已开机”误报成“游戏服务器已可用”。关机通知在实例稳定进入 `STOPPED` 后发送。

## 容器边界

Docker 构建时将 `src/` 和 `src/requirements.txt` 复制进镜像。运行时仅挂载：

- `config.yml`：只读配置；
- `data/`：调度状态、QQ 管理员与订阅目标（`qqbot.json`）、QQ 文案（`message.yml`）
  与健康检查文件；`service.log` 保存轮转后的调度及脱敏 QQ 事件日志。

Monitor 容器以调用 `./monitor.sh` 的用户 UID/GID 运行：`monitor.sh` 注入
`MC_CONTAINER_UID`/`MC_CONTAINER_GID`，静态 `compose.yml` 通过 `user` 使用这两个变量。因此 `data/`
和 `data/message.yml` 直接归该用户所有，不需要 root，也不会再由容器生成 root
所属文件。绕过脚本手工执行 `docker compose` 时需显式设置这两个变量；脚本会拒绝以
root 身份运行。

`.env` 只作为环境变量注入，不复制进镜像；`.dockerignore` 也会将密钥、配置和
运行状态排除在构建上下文之外。

## 源码边界

`src/` 是唯一 Python 源码根目录：`app.py` 负责组装，`config.py` 负责加载配置，
`power.py` 负责电源及 Provider 加载，`schedule.py` 负责日程、状态与服务循环，
`network.py` 只负责 QQ 鉴权和消息收发，`message.py` 只负责读取、校验和渲染
`message.yml`，`qqbot.py` 负责权限和业务服务路由，`targets.py` 负责管理员、
订阅目标与待审批申请。QQ Bot 不读取调度状态，两者只在 `app.py` 中获得同一个电源
控制对象。

`monitor.sh` 只负责运行环境检查、镜像/容器生命周期和在维护操作前后安全停启容器；
涉及 QQ 凭据、网络或 `qqbot.json` 数据结构的操作统一交给 `python -m src.app`。
`app.py` 是依赖组装与进程入口，不实现消息路由、状态机、模板或网络协议。
QQ 日志只记录场景、脱敏短 ID、服务名、发送结果和耗时，不保存消息正文、完整 OpenID
或验证码；`service.log` 每个文件最多 5 MiB，保留 3 个历史文件。

更完整的状态机、模块分层、QQ Bot 命令和安全设计见
[Monitor 架构](../dev/monitor.md)。

`message.yml` 顶部的 `keyboard` 将稳定服务 ID 映射为按钮文案；每条消息的
`keyboard` 是按行排列的服务 ID。修改文案、按钮名称、顺序或显隐不需要改 Python。
例如从 `menu.keyboard` 删除 `power-stop-confirm` 只会隐藏主菜单按钮，不会删除关机
确认服务；当前模板把它放在 `confirm-stop` 消息中。配置引用未实现的服务、未知服务
或无效布局时，Monitor 会在启动阶段直接报错。新增一种真正的业务行为仍需先在
`qqbot.py` 注册对应服务，YAML 不允许执行任意 Python 或 Shell 命令。

## 云电源 Provider

Monitor 从 `.env` 读取云厂商凭据：

```dotenv
POWER_SECRET_ID=
POWER_SECRET_KEY=
MANAGER_API_KEY=
```

前两项与 Manager `.env` 中的 `POWER_SECRET_ID/POWER_SECRET_KEY` 相同：两者控制的是同一
台 Manager 云主机，不需要单独申请第二套密钥。只有一个 Token 时填入
`POWER_SECRET_KEY`，`POWER_SECRET_ID` 留空。`MANAGER_API_KEY` 与 Manager、Terminal
使用同一个值；它是 Manager HTTP API 的 HMAC 密钥，不是云厂商凭据。通过 Terminal 部署
Monitor 时会自动使用 Manager 的密钥和私有 CA；独立部署 Monitor 时，需要从 Manager 的
`data/.env` 复制密钥，并把 `data/tls/ca.crt` 复制到 Monitor 的 `data/tls/ca.crt`。内置 `tencent`
Provider 需要目标实例的 `cvm:DescribeInstances`、`cvm:StartInstances` 和
`cvm:StopInstances` 权限；建议使用只授权目标实例的独立云子账号。自定义 Provider 的
接口约束见 [Monitor 架构中的云电源 Provider](../dev/monitor.md#云电源-provider)。

腾讯云按量计费 CVM 若使用“关机不收费”，建议绑定弹性公网 IP；普通公网 IP 可能在开关机
后变化，导致 DNS 和 Manager API 地址失效。
