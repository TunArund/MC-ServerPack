# Manager 架构

本文描述已经落地的 Manager 内部设计。用户部署、配置和命令见
[Manager 手册](../manager/readme.md)。

## 目标与边界

Manager 是随 Manager 主机启停的单一长生命周控制容器，统一提供整合包管理、
Docker 编排、备份恢复、导入导出、内部调度、任务进度和日志服务。

- 宿主机只依赖 Docker Engine、Compose 插件和 `curl`。
- Python 运行环境和 `manager/src` 固化进 Manager 镜像。
- 所有调用方共用版本化 HTTP JSON API 与同一 Router/业务层。
- `manager.sh` 只负责 Docker 环境检查、控制容器生命周期和 Unix Socket 客户端。
- Terminal 负责跨主机部署和管理端体验；Monitor 只使用受限 API。

## 调用关系

```text
mcsp.sh
  └─ HTTPS + HMAC ──────────────┐
                                 │
Monitor                          ▼
  └─ HTTPS + HMAC ───────> Manager API → router.py → 业务模块 → Docker
                                 ▲
manager.sh                       │
  └─ HTTP over Unix Socket ───┘
```

Manager 内部调度器直接调用业务层，不反向调用 HTTP、Shell 或 `manager.sh`。

## 目录结构

```text
manager/
├── manager.sh
├── src/
│   ├── app.py          # 组装、TCP/Unix API、任务器、调度器和日志
│   ├── router.py       # 路由、校验、scope、幂等和统一响应
│   ├── jobs.py         # 后台任务、互斥、事件与持久化
│   ├── modpacks.py     # 整合包生命周期
│   ├── docker.py       # Docker/Compose 适配层
│   ├── backup.py       # 备份、校验和恢复
│   ├── transfer.py     # 标准归档和分块传输
│   ├── schedule.py     # 聚合调度
│   ├── config.py       # Manager 和整合包配置
│   ├── config_sdk.py   # 共享配置类型校验
│   ├── bootstrap.py    # API 密钥和 TLS 证书初始化
│   ├── manager_sdk.py  # 共享 API 客户端/签名实现
│   ├── power_sdk.py
│   └── providers/
├── runtime/
│   ├── compose.yml
│   ├── manager.dockerfile
│   ├── java8.dockerfile
│   ├── java17.dockerfile
│   ├── java21.dockerfile
│   └── entrypoint.sh
├── data/
│   ├── config.yml
│   ├── .env
│   ├── state.json
│   ├── schedule-state.json
│   ├── run/manager.sock
│   ├── tls/
│   ├── jobs/
│   ├── transfers/
│   └── logs/
└── modpacks/
    ├── .staging/
    ├── template/
    └── <id>/
        ├── config.yml
        ├── compose.yml
        ├── src/
        ├── data/
        └── backups/
```

Manager 镜像复制不可变 `src/`，运行时只挂载 `data/`、`modpacks/` 和 Docker
Socket。Manager 自身 Compose 固定在 `runtime/compose.yml`；整合包 Compose 由
Manager 生成，不得手工维护，也不进入导出归档。

## 业务分层

- `app.py`：读取配置、创建依赖、启动 TCP/Unix HTTP Server、任务执行器、调度器和优雅退出。
- `router.py`：维护 API 路径映射、参数校验、权限 scope、错误映射和响应包络。
- `jobs.py`：维护后台任务、幂等键、资源互斥、事件流和任务持久化；不与时间调度耦合。
- `modpacks.py`：实现 create/start/stop/enable/disable/remove/list/status/console/property。
- `docker.py`：唯一允许调用 Docker Engine/Compose 的业务模块。
- `backup.py` 处理同机回档；`transfer.py` 处理跨服务器迁移，二者语义分离。
- `schedule.py` 聚合扫描所有整合包，不依赖 cron。

## API

基础路径为 `/v1`，JSON 字段使用 `snake_case`。

| 功能 | 方法与路径 | 执行 |
| --- | --- | --- |
| 健康检查 | `GET /v1/health` | 同步 |
| 列出/查看整合包 | `GET /v1/modpacks[/<id>]` | 同步 |
| 创建整合包 | `POST /v1/modpacks` | job |
| 校验配置 | `POST /v1/modpacks/<id>/validate` | 同步 |
| 启动/停止 | `POST /v1/modpacks/<id>/start\|stop` | job |
| 停止全部 | `POST /v1/modpacks/stop-all` | job |
| 开启/关闭自启 | `POST /v1/modpacks/<id>/enable\|disable` | job |
| 删除容器 | `DELETE /v1/modpacks/<id>/container` | job |
| 删除整合包 | `DELETE /v1/modpacks/<id>` | job |
| 控制台 | `POST /v1/modpacks/<id>/console` | 同步 |
| 属性 | `GET/PATCH /v1/modpacks/<id>/properties/<key>` | 同步 |
| 日志 | `GET /v1/modpacks/<id>/logs?lines=N` | 同步 |
| 备份列表/创建 | `GET/POST /v1/modpacks/<id>/backups` | 同步/job |
| 恢复 | `POST /v1/modpacks/<id>/restore` | job |
| 创建导出 | `POST /v1/modpacks/<id>/exports` | job |
| 创建导入上传 | `POST /v1/imports` | 同步 |
| 完成导入 | `POST /v1/transfers/<id>/complete` | job |
| 传输分块 | `GET/PUT /v1/transfers/<id>/chunks/<index>` | 同步 |
| 任务 | `GET /v1/jobs/<id>` | 同步/长轮询 |

同步成功：

```json
{"ok":true,"request_id":"01J...","data":{}}
```

写操作返回 `202 Accepted` 与 `job_id`：

```json
{"ok":true,"request_id":"01J...","data":{"job_id":"01J...","state":"queued"}}
```

错误使用稳定机器码：

```json
{"ok":false,"request_id":"01J...","error":{"code":"modpack_not_found","message":"整合包不存在","detail":{}}}
```

客户端使用 `GET /v1/jobs/<id>?after=<seq>&wait=<seconds>` 增量长轮询。job 状态为
`queued/running/succeeded/failed/cancelled`。写请求支持 `Idempotency-Key`，断线后不得
盲目重提。

## 任务事件与日志

业务模块向任务上下文提交结构化事件：

```json
{
  "seq": 12,
  "time": "2026-10-20T18:30:00+08:00",
  "level": "info",
  "stage": "start",
  "current": 3,
  "total": 4,
  "message": "等待 Minecraft 服务端健康",
  "detail": {"elapsed": 82, "timeout": 300, "health": "starting"}
}
```

Terminal 和 `manager.sh` 渲染同一事件流；Monitor/QQ 只显示提交、完成或失败。
Manager 的业务日志真源在 `data/logs/`，可恢复任务事件在 `data/jobs/`。

## 认证与权限

TCP API 强制 TLS + HMAC。签名覆盖 method、canonical path/query、timestamp、nonce
和 body SHA-256；服务端拒绝过期时间戳和 nonce 重放。共享 SDK 禁止继承环境
HTTP/HTTPS 代理，始终直连指定 Manager 端点并校验 CA。

一个 `MANAGER_API_KEY` 服务于两种身份：

- `admin`：Terminal 的完整管理权限。
- `monitor`：健康、整合包/日志/任务读取和 `stop-all`，不能创建、删除、
  恢复、导入或执行控制台命令。
- Unix Socket：依赖 `0600` 文件权限，等价本机管理员。

Manager API 密钥不得与云厂商 `POWER_SECRET_ID/POWER_SECRET_KEY` 共用。

## 状态与持久化

整合包状态为：

```text
stopped creating starting healthy unhealthy stopping exited unknown maintenance
```

`enable`/`disable` 语义与 systemd 一致，`data/state.json` 是自启状态真源；Docker
restart policy 只是实现细节。创建、导入和恢复过程使用 `maintenance`，不参与自启
和自动备份。

备份只归档 `data/`；标准迁移归档可包含 `config.yml/src/data/backups`。归档
和解压拒绝绝对路径、`..`、重复条目、符号/硬链接和特殊文件，并在同文件系统
的 `modpacks/.staging` 中验证后原子激活。

## 实现状态

Manager 容器化改造已完成并部署：常驻控制容器、Unix/TCP HTTP、TLS/HMAC、
持久化 job/事件、整合包生命周期、enable/disable、备份恢复回滚、标准归档、
分块传输和聚合调度均已接入同一 API。旧业务 CLI、自定义 RPC、`import-dir`、
宿主 Python/cron 入口和 `manager/lib` 已移除。

实际部署命令、生产验证和剩余异常场景见
[deploy-test.md](./deploy-test.md)。
