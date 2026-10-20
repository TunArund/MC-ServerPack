# Manager 手册

Manager 是运行在 Minecraft 主机上的常驻容器服务，负责整合包生命周期、
备份恢复、迁移和内部定时任务。宿主机只需 Docker Engine、Compose 插件和
`curl`，不需要项目 Python 环境。

## 独立部署

```bash
mkdir -p data
cp config.yml.example data/config.yml
cp .env.example data/.env
# 编辑 data/config.yml 和 data/.env
./manager.sh check
./manager.sh install
./manager.sh service status
```

`install` 会构建 Manager 镜像，在临时容器中生成 `MANAGER_API_KEY` 和
`data/tls/` 下的私有 CA/服务器证书，然后启动常驻服务。Manager
容器使用调用 `manager.sh` 的普通用户 UID/GID。

修改 Manager 配置后应用：

```bash
./manager.sh apply-config
```

更换 `host` 后应删除 `data/tls/`，再执行 `install`，使证书 SAN 与新域名
或 IP 一致。如使用公开 CA 签发的证书，将 `server.crt` 和 `server.key`
放入 `data/tls/`；未提供 `ca.crt` 时客户端使用系统信任链。

## Manager 配置

`data/config.yml` 示例：

```yaml
host: mc.example.com
user: minecraft
dir: /home/minecraft/MC-ServerPack-manager
timezone: Asia/Shanghai
api:
  listen: 0.0.0.0
  port: 27016
auto-stop: false
stop-at: '23:05'
power-api:
  provider: tencent
  instance-id: ins-xxxxyyyy
  instance-region: ap-nanjing
modpack:
  auto-stop: false
  stop-at: '23:00'
  stop-notify-elast: 60
  stop-notify-step: 2
  java: [8, 17, 21]
  max-running: 1
  mem-limit: 16g
  start-timeout: 300
  stop-timeout: 180
```

- `host`：Terminal/Monitor 访问 Manager API 使用的域名或 IP，也用于 TLS SAN。
- `timezone`：日志、调度和备份时间的 IANA 时区。
- `api.listen`/`api.port`：HTTPS API 监听地址和端口。
- `modpack.java`：允许的 Java 主版本。
- `max-running`/`mem-limit`：同时运行数和全局内存上限。
- `start-timeout`/`stop-timeout`：启停超时，单位秒。

`data/.env` 保存 `MANAGER_API_KEY` 以及可选的
`POWER_SECRET_ID`/`POWER_SECRET_KEY`。

## 整合包配置

```text
modpacks/<id>/
├── config.yml
├── compose.yml     # Manager 自动生成
├── src/            # 服务端程序、模组和初始配置
├── data/           # 分离持久化的可写路径
└── backups/        # 数据备份
```

整合包 ID 只允许大小写英文字母、数字和减号；`template` 是保留名称。

```yaml
name: 原版服务器
version: 1.20.1
java: 17
mem-limit: 8g
port: 25565
eula: true
backup:
  enabled: true
  interval: 6h
  retain: 7
  quiesce-seconds: 5
read-only:
  enabled: false
  persistent: []
```

默认入口规则：存在唯一 `unix_args.txt` 时使用它；否则要求 `src/` 顶层
恰有一个 JAR，并以 `java -jar <file> nogui` 启动。Forge 安装器会先执行
`--installServer`。Manager 不读取 `run.sh`/`run.bat`/`user_jvm_args.txt`。

`read-only.enabled: false` 时 `src/` 可写；`persistent` 仍决定哪些路径分离到
`data/` 并进入备份。开启只读后，只有 `persistent` 列出的路径可写。

## 宿主命令

```bash
./manager.sh check
./manager.sh install
./manager.sh apply-config
./manager.sh service start
./manager.sh service stop
./manager.sh service status
./manager.sh service logs
```

`manager.sh` 通过 `data/run/manager.sock` 调用 Manager API，不在 Shell 中重复业务逻辑。

## 整合包命令

```bash
# 发现、创建、校验
./manager.sh list
./manager.sh create <id> [--force]
./manager.sh config <id>

# 运行与自启
./manager.sh status <id>
./manager.sh start <id>
./manager.sh stop <id>
./manager.sh stop-all
./manager.sh enable <id> [--now]
./manager.sh disable <id> [--now]

# 日志、控制台和 server.properties
./manager.sh logs <id> --lines 100
./manager.sh console <id> say hello
./manager.sh property <id> online-mode
./manager.sh property <id> online-mode false

# 备份与恢复
./manager.sh backup <id>
./manager.sh backup list <id>
./manager.sh restore <id> [<备份 ID 或文件名>] [--auto-restart]

# 资源清理
./manager.sh remove-container <id>
./manager.sh remove <id> [--force]
```

`start`/`stop` 只改变当前状态。`enable`/`disable` 只改变 Manager 下次启动时
是否自动运行；`--now` 同时执行当前启停。

`remove-container` 只删除容器、网络和自动生成的 Compose，不删除
`config.yml`/`src`/`data`/`backups`。`remove --force` 才允许删除持久数据。

## 备份、恢复与迁移

备份只归档整合包 `data/`。在线备份会执行 `save-off`、`save-all flush`，
并在所有结果中恢复 `save-on`。备份列表显示时间、大小和内容哈希 ID，
恢复时可使用无歧义的 ID 前缀。`--auto-restart` 会自动停服、建立回滚点、
恢复并仅在原先运行时重新启动。

跨主机归档由 Terminal 下载或上传：

```bash
./mcsp.sh manager export <id> ./<id>.tgz
./mcsp.sh manager export <id> ./<id>.tgz --source-only
./mcsp.sh manager export <id> ./<id>.tgz --include-backups
./mcsp.sh manager import ./<id>.tgz [--force]
```

默认归档包含 `manifest.json` 、`config.yml` 、`src/` 和 `data/`。归档不包含
Compose、容器 ID、日志和自启状态。

## 调度与日志

Manager 内部聚合检查备份和定时停服，不依赖 cron。调度状态保存在
`data/schedule-state.json`。

```text
data/logs/service.log     Manager 生命周期与未捕获错误
data/logs/api.log         API 请求和耗时
data/logs/manager.log     整合包业务操作
data/logs/schedule.log    自动备份与定时任务
data/jobs/<job-id>.jsonl  可恢复的任务事件
```

Minecraft 日志用 `logs <id> --lines N` 查看。当前不提供持续 follow。

Manager 的内部架构、API 协议、权限和安全边界见
[Manager 架构](../dev/manager.md)。
