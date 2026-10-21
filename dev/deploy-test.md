# Manager / Monitor 部署测试记录

- 时间：2026-10-20 00:06–00:30（Asia/Shanghai）
- Manager：Debian CVM，Docker 28.5.1；测试开始时服务器处于 STOPPED，使用
  `./terminal/mcsp.sh start manager` 开机。
- Monitor：Ubuntu Lighthouse，Docker 已安装，原 Monitor 容器处于运行状态。
- 测试方式：优先使用 `./terminal/mcsp.sh`、远端 `./manager.sh`、远端 `./monitor.sh`；
  仅在被测命令无法完成诊断时使用 SSH/Docker 直接查看。
- 结论：新 Manager/Monitor 已部署并可运行；测试中发现并修复了 3 个阻断问题，
  仍有若干体验和升级流程问题待处理。

## 已执行的命令

```text
./terminal/mcsp.sh start manager
./terminal/mcsp.sh deploy manager
./terminal/mcsp.sh deploy manager --test
./terminal/mcsp.sh deploy manager --config
./terminal/mcsp.sh manager list
./terminal/mcsp.sh manager status closing-song
./terminal/mcsp.sh manager config closing-song
./terminal/mcsp.sh manager start closing-song
./terminal/mcsp.sh manager stop closing-song
./terminal/mcsp.sh manager stop-all
./terminal/mcsp.sh manager logs closing-song --lines 3
./terminal/mcsp.sh manager console closing-song list
./terminal/mcsp.sh manager property closing-song online-mode
./terminal/mcsp.sh manager backup deploy-test
./terminal/mcsp.sh manager backup list deploy-test
./terminal/mcsp.sh manager restore deploy-test
./terminal/mcsp.sh manager export deploy-test /tmp/deploy-test.tgz
./terminal/mcsp.sh manager import /tmp/deploy-test.tgz --force
./terminal/mcsp.sh manager create/remove/enable/disable deploy-test
./terminal/mcsp.sh deploy monitor
./terminal/mcsp.sh deploy monitor --test
./terminal/mcsp.sh deploy monitor --config
./terminal/mcsp.sh monitor status/start/stop
./terminal/mcsp.sh monitor test-alert
./terminal/mcsp.sh status manager
./terminal/mcsp.sh status monitor
./manager.sh list/status/config/start/stop/stop-all/logs/property/backup list/export/create/remove
./monitor.sh check/status/logs/install
```

## 已修复的问题

### 1. create/import 跨文件系统失败（阻断）

**现象**：`./manager.sh create deploy-test` 返回：

```text
任务开始执行
Manager 内部错误
```

没有堆栈，无法从日志定位。

**原因**：Manager 容器把 `data/` 和 `modpacks/` 分成两个 bind mount。
`ModpackManager.create()` 和 `import_pack()` 把 staging 放在 `data/transfers`，
最后 `os.replace/rename` 到 `modpacks/<id>`，跨文件系统触发
`OSError: [Errno 18] Cross-device link`。

**修复**：

- create/import 的 staging 改到 `modpacks/.staging`，与目标同一文件系统。
- `JobStore` 捕获未预期异常时增加 `LOGGER.exception(...)`，以后内部错误会记录堆栈。
- 已重新部署并验证 create/remove/import（带顶层 JAR 的归档）成功。

### 2. 旧本地 `manager/data` 被上传，Manager 证书域名错误（阻断）

**现象**：Monitor 日志：

```text
CERTIFICATE_VERIFY_FAILED: Hostname mismatch,
certificate is not valid for 'mc.tunarund.top'
```

检查发现 Manager 实际证书 SAN 是 `DNS:manager.example`，不是
`DNS:mc.tunarund.top`。

**原因**：`RuntimeDeployment.preserve` 只保护远端目录，`sync_tree` 仍会读取并上传
本地 `manager/data` 下的文件。测试机上残留的本地 `manager/data/tls/server.crt`
和 `server.key` 被同步到远端，bootstrap 看到证书已存在便直接复用，导致证书
域名来自旧的本地测试证书。

**修复**：

- `manager/.mcspignore` 增加 `data` 与 `data/*`，本地 `data/` 不再参与源码同步；
  配置和 `.env` 仍由部署逻辑单独上传。
- 清除本地和远端旧 `data/tls` 后重新部署，Manager 重新生成
  `DNS:mc.tunarund.top` 证书；Monitor 重新上传新 CA。
- 已用 Terminal API、Monitor SDK 和 Monitor 实际请求验证 TLS/HMAC 均正常。
- 建议：部署流程在生成证书后校验 SAN 与 `manager.host` 一致，并在不一致时直接报错。

### 3. Manager 内部任务错误缺少堆栈（可诊断性）

**现象**：create 失败只显示“Manager 内部错误”，`data/logs/manager.log` 只有
`state=failed code=internal_error`。

**修复**：JobStore 在失败分支记录 `LOGGER.exception`，堆栈写入 Manager 日志和
容器 stdout；重新部署后可用。

### 4. Manager SDK 受环境代理干扰

**现象**：设置 `HTTP_PROXY`/`HTTPS_PROXY` 时，SDK 可能把指定的 Manager API
请求发往代理，本机 TLS 集成测试报 `SSL EOF`。

**修复**：三份字节一致的 `manager_sdk.py` 统一使用禁用环境代理的
HTTPS opener；Manager API 按配置端点直连，仍固定验证私有 CA。

## 待处理问题

### 升级流程

1. **旧 Manager 容器不会自动停止/清理**。测试时旧服务名为 `mcsp-manager-api`，
   新服务名为 `mcsp-manager`；直接 deploy 会让两个 Manager 同时运行。本次先手动
   停止旧 pack，再 `docker rm -f mcsp-manager-api`。建议 deploy manager 增加升级
   预检，检测旧容器/旧目录并提示或自动切换。
2. **旧状态没有迁移**。旧 Manager 的 `.schedule-state.json`、`manager.log`、
   `schedule.log` 留在部署根目录；新 Manager 使用 `data/`。新 `state.json` 中
   `closing-song` 的 enabled 为 false，部署后不会自动启动，需要手动 start/enable。
   建议文档明确这是一次性迁移，或提供状态迁移/首次启动提示。
3. **旧镜像没有清理**。旧 Manager 的 `mc-serverpack-manager-python:local` 和旧
   `mc-java:*` 镜像仍保留；Manager install 每次全量构建，也没有 prune。Monitor
   侧同样观察到了多个 dangling 镜像，`prune_images` 未能清掉全部旧构建。
4. **部署过程回显不足**。`deploy manager` 在构建镜像、运行 bootstrap 时长时间
   没有新输出，最后才显示“Manager 安装完成”；用户无法判断是否卡住。建议把
   bootstrap 的“已生成密钥/证书”和镜像构建阶段透传到 Terminal。

### 命令体验

5. **日志输出仍是 JSON 包络**。`./terminal/mcsp.sh manager logs` 和
   `./manager.sh logs` 输出 `{"text":"...\n..."}`，换行被转义；应直接打印文本。
6. **Manager 端 export 不下载文件**。`./manager.sh export <id>` 只返回
   transfer 元数据，没有生成本地文件；Terminal 的 `mcsp.sh manager export` 才下载。
   `manager.sh` 也没有 `--source-only/--include-backups/--auto-stop`。
7. **export/import 成功时没有明确结论文案**。export 成功后除时间戳外无输出；
   import 成功只显示任务进度。建议输出目标文件/整合包 ID 的明确结果。
8. **failed job 信息重复**。`manager.sh` 在事件循环中打印一次错误，最后又把完整
   JSON 打到 stderr，用户会看到两遍相同错误。
9. **进度重复**。远端 `manager.sh wait_job` 不去重，start 时重复打印
   “等待 Minecraft 服务端健康”几十次；Terminal 侧已去重。建议复用同一套事件
   去重逻辑。
10. **Monitor 无运行中整合包时刷错误日志**。Manager 已 RUNNING 但没有 pack 运行
    时，`QQBot.notify` 抛 `Manager 尚无运行中的整合包`，Monitor 每 30 秒记录一次
    完整 traceback（本次观察到 20 次）。此时应静默等待或只记 debug，不应作为
    失败告警。
11. **`./terminal/mcsp.sh status monitor` 文案误导**。它实际只查询容器状态，却输出
    “Monitor 容器操作已提交：status”。
12. **`monitor.sh` 缺少 Manager API 连通性检查**。排查 TLS/密钥问题只能进容器
    用一段 Python 调 `ManagerSdk`；建议增加 `./monitor.sh check-manager`。
13. **import 对无 JAR 的整合包失败**。`create` 生成的模板没有 JAR，可以创建成功；
    把同样的整合包导出后再 import，会在 `manager.configure` 阶段报
    “自动启动要求 src/ 中恰有一个顶层 JAR”，导致导入失败。两者行为不一致，
    至少应允许导入后再补 JAR，或给出明确的前置条件。
14. **服务器 STOPPED 时 deploy 只报原始 SSH 错误**。测试开始时 Manager 是关机
    状态，`deploy manager --test`/`deploy manager` 直接连接失败。建议部署前检查
    电源状态并提示先执行 `./terminal/mcsp.sh start manager`，或提供自动开机选项。

## 已验证功能

- 独立部署：远端 `./manager.sh install` 成功（镜像构建 + bootstrap + compose），
  远端 `./monitor.sh install` 成功（复用已有镜像和密钥/CA）。
- Manager 控制容器、bootstrap 自动生成密钥/证书、Terminal 取回密钥/CA。
- Terminal Manager API：list/status/create/remove/enable/disable/start/stop/
  stop-all/config/logs/console/property/backup/backup list/restore/export/import。
- 部署后恢复生产：`enable closing-song` 后 `start closing-song`，状态 healthy。
- 远端 `manager.sh`：check/service status/service logs/list/status/config/start/
  stop/stop-all/logs/property/backup list/create/remove/export。
- Monitor：部署、容器 start/stop/status、test-alert、reset-targets、add-admin
  私聊验证码流程（用户实测成功）、Manager API 读取和 `stop_all`（用 Monitor 容器内
  的 SDK 直接验证，返回 `{'stopped': ['closing-song']}`）。
- 公开开机通知：Manager 有运行中的整合包后，Monitor 成功向群发送通知。
- `./terminal/mcsp.sh config check`、`deploy manager --test`、`deploy monitor --test`、
  `deploy manager --config`、`deploy monitor --config`。

## 观察

- 第一次部署后，Manager 服务日志显示 TCP 已监听 `0.0.0.0:27016`，但 Terminal 访问
  公网地址先是 connection refused，随后变为 timeout；重新部署并修复证书后恢复。
  由于同一时间 Monitor 也未连通，未确认是云安全组、NAT 还是旧证书状态导致，后续
  部署应保留 Manager 端 `curl https://127.0.0.1:27016` 与公网访问的对照检查。
- Terminal 部署时 Manager 镜像构建和 bootstrap 的输出被折叠成一行“安装完成”；
  直接运行 `./manager.sh install` 则能看到完整构建日志。两者行为不一致。

## 用户补充与后续改造方向（2026-10-20）

### 环境

- 云端安全组已放行 `25500-27099`。Manager API 使用 27016，Minecraft 使用 27015；
  先前部署初期出现的公网 connection refused/timeout 与放行时间有关，当前已正常。
- 大部分用户可能直接使用公网 IP 而不是域名；TLS 证书的 SAN 设计需要按这个前提重新
  评估（见下）。

### 回显与日志统一改造

部署和日常命令目前混用多种输出：阶段行、重复事件、原始 JSON、完整任务事件、Docker
构建日志。建议先定一套规范，再改实现：

- 默认人类可读，`--json` 输出机器可读，`--quiet` 只输出结果/错误。
- 统一阶段格式：`[阶段 当前/总数] 消息`；重复事件去重；任务结束输出一句明确结果，
  不再同时打印事件列表和完整 JSON。
- 错误只显示一次；详细堆栈放到 `--verbose` 或日志，不在默认回显中刷屏。
- Terminal、远端 `manager.sh`、部署流程使用同一套文案/阶段名；部署时显示
  “同步源码 → 构建镜像 → 生成密钥/证书 → 启动/健康检查”的实时阶段和耗时。
- 日志格式统一时间、级别、组件、request/job ID、目标、消息；日志查询命令直接输出
  Minecraft 原始文本，不返回 JSON 包络。
- 轮转可参考按日目录/文件的做法，但建议使用 Python `TimedRotatingFileHandler`
  或现有的内部 schedule 统一做每日切分和保留，不再引入第二套调度器；同时保留
  按大小兜底，避免单日暴涨。

### QQ 文案

- 首行空行已通过“带按钮消息使用标题式 Markdown、无按钮消息使用纯文本”解决；
  纯文本附带 keyboard 虽被接口接受，但 QQ 客户端不会显示按钮。
- 文案按旧设计调整：正文不使用 markdown 语法，强调用 emoji；带按钮的消息仍使用
  `msg_type=2`，但正文保持纯文本。`menu`、告警、公开通知和订阅申请等模板已去除
  `**`、`#`、反引号。
- 静默按钮保持 `1h/2h/5h`；`alert-test` 正文为
  “这是一条测试告警，不会操作游戏服务器。点击按钮忽略告警1/2/5h。”
- 已重新部署 Monitor 并发送测试告警；本地模板和远端 `data/message.yml` 已同步。
- `data/message.yml` 会被部署保留，源码里的文案修改不会自动覆盖已部署文件；
  需要在线热更新或提供明确的模板升级/重置命令。

### TLS 与 IP 地址

- 当前 bootstrap 已支持 `DNS:` 和 `IP:` 两种 SAN；如果用户直接用公网 IP，
  默认私有 CA + Terminal/Monitor 固定信任 CA 仍然可用。
- 需要重新考虑的点：
  - 服务器停启后公网 IP 可能变化。当前证书已存在时不会重新生成，SAN 会过期；
    建议 bootstrap 校验现有证书 SAN 是否包含当前 host，不匹配时提示删除/自动重建。
  - 如果 Monitor 使用另一个地址访问 Manager，该地址也必须进入 SAN；目前只保证
    `manager.host`，Monitor 的覆盖地址没有自动加入。
  - 可考虑同时写入 `manager.host`、Monitor 覆盖地址和本地回环地址作为 SAN，
    并为 IP 场景增加自动化测试。
  - 公网 CA 对裸 IP 不友好，项目默认的私有 CA + CA 固定信任是更现实的方向。

### enable/disable

已实现：

- API：`POST /v1/modpacks/{id}/enable|disable`，请求体支持 `{"now": true}`。
- Terminal：`./terminal/mcsp.sh manager enable|disable <id> [--now]`。
- 远端：`./manager.sh enable|disable <id> [--now]`。
- 状态保存在 `data/state.json`；Manager 启动时会 reconcile 已启用整合包。
- 本次部署测试已验证 enable/disable 不立即启停，并实测 `disable --now` 停服、
  `enable --now` 启服；“停机后重启自动拉起”还需要一次完整回归。

### 机器人整合包管理（TODO）

可以复用 Monitor 已经复制过去的 `ManagerSdk`，在 QQ 服务层增加：

- `整合包列表`：列出 ID、名称、运行状态、端口。
- `状态 <整合包>`：查询指定整合包状态。
- `日志 <整合包> [行数]`：读取指定整合包最近日志。
- `启动 <整合包>` / `停止 <整合包>`：管理员操作，提交后等待任务结果。
- `自启 <整合包>` / `取消自启 <整合包>`：管理员操作，对应 enable/disable。
- 需要处理：ID/名称匹配与歧义、管理员权限、停止前确认、任务进度/超时、消息长度
  截断、以及把同步 Manager 调用放进 `asyncio.to_thread`，避免阻塞 QQ 事件循环。

## 未测试

- 定时关机 / QQ 人工关机的完整服务器关机链路（本次只验证了 stop-all 和电源查询）。
- QQ 拒绝订阅及定时任务触发尚未线上回归；管理员验证、群聊审批、菜单、确认关机、
  告警及静默按钮已经真实 QQ 单聊/群聊验证。
- 备份异常恢复、Docker daemon 重启、Forge 长启动超时等容器级异常场景。

## 测试后状态

- `closing-song` 已迁移并设置 enable；Manager 服务器按游玩窗口启停。
- Manager 数据、备份和世界数据保留；旧配置备份位于 Manager 主机的
  `/home/steam/mcsp-manager-config-*.tgz`。
- Monitor 容器运行中；QQ 管理员与订阅数据保留。
- 当前代码的 296 项单元测试通过。
