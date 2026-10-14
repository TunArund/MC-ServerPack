# Manager

manager 部署到 mc-server，负责整合包配置、Docker 生命周期、日志、备份与恢复。

## 运行环境

需要 Python 3.8+、PyYAML、Docker Compose 和 cron。Debian/Ubuntu 可由部署流程
自动安装；其他 Linux 发行版预先安装依赖后也可使用。发行包中的 `manager/lib/`
会随 `manager.sh` 一起同步，不依赖 terminal 使用的操作系统。

## 配置

根 `config.yml` 中的 manager 段描述 SSH 目标和全局限制：

```yaml
manager:
  user: steam
  host: 10.0.0.5
  dir: /home/steam/MC-ServerPack/manager
  java: [21]                  # 部署时构建；旧 Forge 可添加 8 或 17
  modpack:
    max-running: 1
    overnum-action: reject
    mem-limit: 12g
    start-timeout: 5m
    stop-timeout: 3m
```

单个整合包结构：

```text
manager/modpacks/<ID>/
├── config.yml                  # Java、内存、端口、只读与持久化、备份
├── compose.yml                 # 自动生成
├── src/                        # 导入的服务端文件
├── data/                       # 独立持久化内容
└── backups/                    # world/ 轮转备份
```

整合包配置示例：

```yaml
name: 我的整合包
version: 1.0
java: 17
memory: 8g
port: 25565

backup:
  enabled: true
  interval: 6h
  retain: 7

read-only:
  enabled: false
  persistent:
    - config/
    - world/
    - logs/
    - crash-reports/
    - server.properties
```

## 只读与持久化

`read-only.enabled` 默认为 `false`：整个 `src/` 可写，遗漏路径不会阻止服务端启动。
`persistent` 中的路径仍由 `data/` 覆盖挂载，更新或重新部署 `src/` 时不会丢失。

设为 `true` 后，`src/` 整体只读，`persistent` 同时成为可写例外。严格模式适合已经
调试成熟的整合包；如果出现 `Read-only file system`、`AccessDeniedException`、
`FileSystemException` 或 `failed to create/write`，通常是遗漏了运行时写入路径。

常见持久化路径包括：

- `config/`：Forge 和模组可能保存或规范化 TOML/JSON。
- `world/`、`logs/`、`crash-reports/`。
- `backups/`：FTB Backups 等模组自己的备份目录。
- `local/kubejs/cache/`、Forge 在 `libraries/` 下生成的 `.cache` 文件。
- `server.properties`、`eula.txt`、玩家名单、权限和缓存 JSON。
- 特定模组产生的 `cache/`、`database/`、`storage/` 或 `data/`。

目录以 `/` 结尾，文件不加 `/`。首次配置会从 `src/` 初始化尚不存在的持久化内容，
已有 `data/` 永远不会被覆盖。`modpack scan <ID>` 可以查找名称包含 `cache` 的候选
路径。宽松模式下，未列入 `persistent` 的运行时修改会留在 `src/`，下次重新部署
整合包时可能被替换。

## 备份与恢复

备份只包含 `data/world/`，按 `retain` 轮转。恢复前必须停止整合包，恢复时会先备份
当前世界。自动备份由 mc-server 用户的 crontab 执行。

## 命令

```text
modpack create <ID>
modpack import [--force] <ID> <目录、文件或URL>
modpack scan <ID>
modpack deploy <ID>

manager remove [--force] <ID>
manager config <ID>
manager start|stop|restart <ID>
manager status [ID]
manager logs [-f] <ID> [行数]
manager backup <ID>
manager restore <ID> [备份文件名]
```

`modpack` 命令在个人 PC 执行；`manager` 命令通过 SSH 调用远端 `manager.sh`。
