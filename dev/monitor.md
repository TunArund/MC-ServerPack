# Monitor 架构

本文描述已落地的 Monitor 内部设计。用户部署、配置和 QQ 订阅流程见
[Monitor 手册](../monitor/readme.md)。

## 目标与边界

Monitor 是部署在 7×24 小时运行主机上的长生命周容器，负责 Manager 服务器的
电源调度、状态检查、告警、公开通知和 QQ 交互。

- Monitor 只通过云厂商 Provider 操作主机电源。
- Monitor 只通过受限 Manager API 读取整合包状态、日志并在关机前执行
  `stop-all`。
- Monitor 不使用 SSH/TAT，不调用 `manager.sh`，不挂载 Manager 的 Docker Socket。
- `monitor.sh` 是宿主机薄入口，只负责环境检查、镜像/容器生命周期和维护命令。
- Python 环境和 `monitor/src` 固化进镜像；宿主机只需 Docker Engine 和 Compose。

## 调用关系

```text
QQ Gateway / QQ HTTP API
            ▲
            │ WebSocket + HTTPS
            ▼
      network.py
            │ Incoming / Outgoing
            ▼
       qqbot.py ──────> message.py ──> data/message.yml
            │                    targets.py ──> data/qqbot.json
            │
            ├──> schedule.py ──> data/state.json
            ├──> power_sdk.py ──> 云厂商 Provider
            └──> manager_sdk.py ──> Manager HTTPS API

monitor.sh ──> Docker Compose ──> app.py 组装以上服务
```

`app.py` 是唯一进程入口，组装电源、调度、Manager SDK、QQ 网络和持久化存储。
它不实现消息路由、模板语法或状态机分支。

## 目录结构

```text
monitor/
├── monitor.sh
├── Dockerfile
├── compose.yml
├── config.yml
├── .env
├── src/
│   ├── app.py          # 依赖组装、进程入口与日志
│   ├── config.py       # 配置与密钥校验
│   ├── config_sdk.py   # 共享配置类型校验
│   ├── schedule.py     # 电源日程、状态机和聚合服务循环
│   ├── qqbot.py        # QQ 权限、订阅和业务服务路由
│   ├── network.py      # QQ token、WebSocket 和 HTTP 收发
│   ├── message.py      # YAML 校验、热加载和渲染
│   ├── message.yml     # 默认文案、按钮文案和布局
│   ├── targets.py      # 管理员、订阅目标和待审批申请
│   ├── manager_sdk.py  # 受限 Manager API 客户端
│   ├── power_sdk.py    # 通用云电源状态机
│   ├── providers/
│   └── requirements.txt
└── data/
    ├── state.json       # 调度、人工操作、告警静默和通知状态
    ├── qqbot.json       # 管理员、订阅和申请
    ├── message.yml      # 运行时可热加载模板
    ├── tls/ca.crt       # Manager 私有 CA
    ├── heartbeat
    ├── image-source-hash
    └── service.log
```

生产镜像复制 `src/`，运行时只挂载只读 `config.yml` 和可写 `data/`，不挂载
源码；`.env` 通过 Compose `env_file` 注入环境变量。容器使用调用 `monitor.sh`
的普通用户 UID/GID。

## 业务分层

- `network.py` 只处理 QQ 认证、Gateway 事件和消息发送，并将单聊/群聊归一为
  `Incoming`/`Target`。
- `message.py` 只校验和渲染 YAML；不访问网络、电源或订阅存储。
- `qqbot.py` 负责命令识别、权限、关机二次确认、订阅审批、告警聚合和服务路由。
- `targets.py` 是 QQ 身份与订阅的持久化真源，不依赖网络传输。
- `schedule.py` 管理电源日程和持久化状态；QQ Bot 通过注入的函数调用它，不直接
  读写 `state.json`。
- `power_sdk.py` 负责 Provider 加载和幂等的 status/start/stop 状态机。

YAML 可调整文案、按钮名称、顺序和显隐，但不得执行 Python/Shell。新增业务行为
必须先在 `qqbot.py` 注册稳定服务 ID，再由 `message.yml` 引用。

## 电源调度状态机

Monitor 每 30 秒执行一次聚合检查：

1. 读取服务器当前状态。
2. 得出当前时间线上最近的已启用开/关机事件。
3. 未执行事件按 Provider 幂等语义提交；关机前必须等待 Manager `stop-all`
   成功。
4. 保存完成/取消事件、人工操作、告警静默和已通知的稳定电源状态。
5. 当实际状态与日程不一致时发送私聊告警；恢复后发送一次恢复通知。
6. 稳定进入 `RUNNING` 或 `STOPPED` 后，向已批准群聊发送公开通知。

首次创建 `state.json` 时会把当时最近事件标记为已处理，不补执行部署前错过的
日程。之后的进程重启会重试当前时间线上尚未完成的最近事件。取消自动开机
仅取消下一次事件，不禁用配置中的长期日程。

人工关机不等于取消下一次自动开机。Monitor 会记录人工操作，以便在后续
日程不一致告警中说明原因，但不会暗中修改预订。

## QQ 身份与订阅

Monitor 只处理两类入站事件：

- `C2C_MESSAGE_CREATE`：QQ 单聊。
- `GROUP_AT_MESSAGE_CREATE`：群内 @ 机器人消息。

初始管理员由 `monitor add-admin` 生成四位小写十六进制验证码，用户私聊发送
完全匹配的验证码后写入 `qqbot.json`。后续目标管理不依赖命令行：

1. 任意单聊用户或群内成员发送 `订阅 [昵称]`。
2. Bot 向所有管理员私聊发送带六位申请编号的审批消息。
3. 任意管理员在私聊中点击按钮，或发送 `批准 <编号>` / `拒绝 <编号>`。
4. 已批准单聊目标接收异常告警；已批准群聊只接收公开开/关机通知。

`union_openid` 可能为空，不能作为唯一的跨场景身份依据。群成员在已订阅群内发送
`申请管理`，现有管理员私聊批准后，Monitor 才将该群的
`group_openid + member_openid` 显式绑定到申请人。群主和订阅申请人不会自动获得
管理权限。

## QQ Bot 可用命令

命令与按钮文案完全匹配，默认值来自 `message.yml` 的 `keyboard`。如果运行时修改
了按钮文案，文本命令也随之改变。群聊中必须 @ 机器人。

| 默认命令 | 使用者 | 场景 | 行为 |
| --- | --- | --- | --- |
| `订阅 [昵称]` | 任意用户 | 单聊或群聊 | 申请订阅；群聊可填便于识别的备注 |
| `批准 <六位编号>` | 管理员 | 仅单聊 | 批准订阅申请 |
| `拒绝 <六位编号>` | 管理员 | 仅单聊 | 拒绝订阅申请 |
| `菜单` | 已授权目标 | 单聊或群聊 | 查看服务器状态；非管理员获得只读菜单 |
| `申请管理` | 群成员 | 已订阅群聊 | 向现有管理员提交群聊管理权限申请 |
| `状态` | 管理员 | 单聊或已绑定群聊 | 列出全部已启用整合包的名称、版本和状态 |
| `日志` | 管理员 | 单聊或已绑定群聊 | 按整合包名称生成按钮，选择后返回最近 10 行 |
| `开机自启` | 管理员 | 单聊或已绑定群聊 | 选择尚未启用的整合包并 enable |
| `取消自启` | 管理员 | 单聊或已绑定群聊 | 选择已启用整合包并 disable |
| `开机` | 管理员 | 单聊或可识别管理员身份的群聊 | 提交 Manager 服务器开机 |
| `关机` | 管理员 | 单聊或可识别管理员身份的群聊 | 建立 60 秒二次确认，不立即关机 |
| `确认关机` | 管理员 | 与原请求相同会话/用户 | 先执行 Manager `stop-all`，成功后关机 |
| `取消预订` | 管理员 | 单聊或可识别管理员身份的群聊 | 取消下一次自动开机 |
| `恢复预订` | 管理员 | 单聊或可识别管理员身份的群聊 | 恢复已取消的下一次自动开机 |
| `1h` / `2h` / `5h` | 管理员 | 单聊或可识别管理员身份的群聊 | 暂停私聊异常告警；检查和定时任务继续 |

未订阅且非管理员的目标，除 `订阅` 外其他消息会被忽略。已批准但非管理员的
目标只能使用 `菜单`，尝试其他服务会收到权限提示。重复发送 `关机` 不会代替
`确认关机`。

## 消息模板与热加载

`src/message.yml` 是镜像内默认模板。首次运行会复制到 `data/message.yml`，之后
每次渲染前检查文件签名并热加载。新文件无效时保留上一份有效模板，仅记录告警。

```yaml
keyboard:
  menu: 菜单
  power-start: 开机

menu:
  markdown: |-
    ## 服务器小管家
    🎮 游戏服务器当前状态：{state}
  keyboard:
    - [power-start, power-stop]
```

`keyboard` 顶层将稳定服务 ID 映射到按钮/文本命令。每条消息的 `keyboard` 是按行排列的
服务 ID。删除某条消息中的按钮只会隐藏入口，不会删除对应服务。

当前 QQ 客户端只会为 `msg_type=2` 的 Markdown 消息显示自定义键盘；虽然接口字段
允许 `msg_type=0 + content + keyboard`，真实客户端不会显示其中的键盘。请求体只发送
`markdown + keyboard`，不发送可选的 `content` 字段；Markdown 正文以标题起始，避免
普通段落产生顶部留白。无按钮的普通消息使用 `msg_type=0 + content`。
该结论以 `bot.q.qq.com/wiki/develop/api-v2` 现行文档与真实客端验证为准。
当前参考：[QQ 消息交互概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/overview.html)、
[单聊消息接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)。

## 通知、告警与频控

- 异常告警和恢复通知发送给所有管理员和已批准单聊目标。
- 公开开/关机通知只发送给已批准群聊。
- 开机通知必须等待 Manager API 确认至少一个整合包处于 running/healthy，并从
  API 生成实际名称和 `host:port`。
- 告警队列在 60 秒窗口内按类型聚合；同一状态不一致类型只报告窗口内最新状态。
- QQ 平台频控由每个用户自己的 Bot 配额承担；Monitor 不对高频业务做广播。

## 云电源 Provider

`provider` 是只允许大小写字母、数字和下划线的适配器 ID。例如 `provider: xxx_aaa`
会加载 `src/providers/xxx_aaa.py`。`tencent` 是内置适配器。

自定义 Provider 必须实现 `status`、`start`、`stop`，签名统一为：

```python
def status(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict: ...
def start(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict: ...
def stop(instance_id: str, region: str, secret_id: str, secret_key: str) -> dict: ...
```

返回值由 `power_sdk.py` 统一解析为云实例状态。Provider 只适配厂商 API，不复制等待、
幂等和业务判断。

## 安全与日志

- `.env` 只作为容器环境变量，不进入镜像。
- `MANAGER_API_KEY` 与 Manager/Terminal 使用同一值，但 Monitor 身份只拥有读取和
  `stop-all` scope。
- `POWER_SECRET_ID`/`POWER_SECRET_KEY` 可与 Manager 使用同一组云 AK/SK，但不得
  与 `MANAGER_API_KEY` 共用。
- QQ 日志不记录消息正文、验证码和完整 OpenID；只记录场景、脱敏短 ID、服务、
  结果和耗时。
- `service.log` 按 5 MiB 轮转，保留 3 个历史文件，并使用 Monitor 配置时区。

## 实现状态

Monitor 已以非 root 长生命周容器部署，完成电源日程、安全关机链路、告警聚合、
单聊/群聊分类通知、验证码管理员、订阅审批、模板热加载和 Manager API 受限调用。
真实部署结果和尚未回归的异常场景见 [deploy-test.md](./deploy-test.md)。
