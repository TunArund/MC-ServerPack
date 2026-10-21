# 开发准则

## 基准

- 当前项目以个人使用为主，允许破坏性变更，不为未发布的兼容性增加双路逻辑。
- 保持目录和抽象简洁；没有明确责任边界时不新建源码文件。
- 文档不得把计划或未验证设计写成已发布行为。

## 文档边界

- `/readme.md`：项目总览与跨组件命令。
- `/terminal/readme.md`：Terminal 安装、配置、日志和独立使用。
- `/manager/readme.md`：Manager 独立部署、配置、整合包结构和命令。
- `/monitor/readme.md`：Monitor 独立部署、调度、QQ Bot 和消息模板。
- `/dev/manager.md`：Manager 内部架构、目录、API、任务、安全和持久化设计。
- `/dev/monitor.md`：Monitor 状态机、QQ 分层、订阅权限、命令和通知设计。
- `/dev/release/`：发行设计、构建源码、暂存文件与发行产物的唯一目录；入口为
  `release.py`，设计见 `readme.md`。
- 本文：通用开发约束、测试环境和 TODO。
- `/dev/deploy-test.md`：真实部署验收记录，不作为用户手册。

## 边界与依赖

- Terminal 只负责配置、远端环境准备、部署和 API 编排；不实现 Manager/Monitor
  内部业务。
- Manager 和 Monitor 宿主机只依赖 Docker/Compose；Python 与源码固化在各自镜像中。
- `manager.sh`/`monitor.sh` 是宿主机薄入口，不复制 Python 业务逻辑。
- 业务模块不直接 `print()`；向调用方返回结构化结果或提交结构化任务事件。
- Manager 的 Docker Socket 只挂载给 Manager 控制容器，不挂载给 Monitor 或 Minecraft
  容器。

## 共享源码

- `manager/src/manager_sdk.py`、`lib/manager_sdk.py`、`monitor/src/manager_sdk.py` 必须字节一致。
- Terminal、Manager、Monitor 的 `power.py` 和 Provider 协议保持相同，以便直接复制同步。
- 共享文件只在一处作语义设计，同步到其他位置后必须运行边界测试。

## 配置和密钥

- Manager 和 Monitor 各自从顶层 `timezone` 读取 IANA 时区，用于调度、日志、备份和消息。
- Manager 和 Monitor 控制同一服务器时，各自 `.env` 中使用同值的
  `POWER_SECRET_ID`/`POWER_SECRET_KEY`。只有 Token 时填入 KEY。
- `MANAGER_API_KEY` 与云 AK/SK 分离；Terminal 和 Monitor 复用同一 Manager API 密钥，
  但以不同身份获得不同 scope。
- 日志不记录密钥、签名、验证码、完整 OpenID 或敏感请求体。

## 回显与日志

- Terminal 的 Renderer 是人类回显唯一边界：最终数据写 stdout，进度、提示、警告和错误
  写 stderr；TTY 可以覆盖尾窗，非 TTY 逐行输出且不使用 ANSI。
- `--json` 使 stdout 只包含一个 `{ok,result|error}` JSON 文档；非 TTY 不显示进度。
  `--quiet` 隐藏进度和成功提示，但保留查询数据与错误；两者互斥。
- Terminal 退出码：`0` 成功、`2` 参数或配置错误、`3` 认证或权限错误、`4` 外部服务
  不可用、`5` 超时、`70` 未预期内部错误、`130` 用户中断。
- 本地命令和 SSH 命令共用一个有界尾窗实现。不得恢复 stdout/stderr Tee、操作临时日志、
  自制锁文件或把完整命令回显当作诊断日志。
- Job 事件是业务状态，不是日志。事件的 `type`、`code`、`stage` 使用稳定机器字段；
  Terminal 与 `manager.sh` 通过共享 `manager_sdk.format_job_event()` 使用相同文本规则。
- `manager.sh` 不使用 grep/sed 解析 JSON；解析交给 Manager 容器内的 `src.job_bridge`，
  宿主机仍只依赖 Docker、Compose 和 curl。
- 三组件只在进程入口配置一次标准库 `logging`。日志为 UTF-8 JSON Lines，默认 INFO，
  `5 MiB × 3` 轮转；异常使用 traceback，结构化字段通过 `extra` 传递并递归脱敏。
- 日志真源为 `terminal/logs/terminal.log`、`manager/data/logs/service.log`、
  `manager/data/logs/api.log`、`monitor/data/service.log`。容器 stderr 使用同一 JSON 格式。
- `lib/observability.py` 是配置、上下文、格式化和脱敏的源码真源；三个独立组件在 `src/`
  携带字节一致副本，并由边界测试约束。

## Monitor 约束

- QQ Bot 协议、事件和 API 实现唯一文档来源是
  `https://bot.q.qq.com/wiki/develop/api-v2/`；不使用过时 GitHub 文档、旧版示例或其他域名推断行为。
- 镜像复制 `monitor/src`，运行时只挂载只读 `config.yml` 和可写 `data/`，不挂载源码。
- QQ 分层为 `network.py` 传输、`message.py` 渲染、`qqbot.py` 业务路由、`targets.py`
  目标和审批；`message.yml` 只映射稳定服务 ID，禁止动态执行代码。
- 管理员通过 `monitor add-admin` 的私聊验证码初始化；订阅和审批后续在 QQ 完成。
- 单聊使用 `user_openid`，群聊使用 `group_openid`；不得把群成员 OpenID 当作单聊 OpenID。

## 测试

单元测试：

```bash
python3 -m unittest discover -s dev/tests -q
```

测试必须保持独立、快速且不访问真实云 API。需要环回网络的 TLS 集成测试应
只监听随机本机端口。变更大时要及时删除已无意义的兼容测试。

真实环境与最近部署结果见 [deploy-test.md](./deploy-test.md)。

## TODO
- MC-ServerPack/manager/runtime迁移到manager/data/{xxx.dockerfile,.dockerignore}
减少目录，并扁平化
- 将 Terminal、Manager 与 Monitor 重复的 Docker 部署逻辑提炼到 `/lib`，各运行时只保留
  参数和生命周期编排；在统一部署契约前不提前复制或增加兼容分支。
- 完善 Manager 升级预检、旧状态迁移、旧镜像清理和证书 SAN 变更检测。
- 完成 QQ 整合包管理：列表、状态、日志、启停和 enable/disable；所有写操作仅管理员可用。
- 实现 COS 备份，覆盖恶意归档、分块中断续传、配额、恢复回滚和日志脱敏。
