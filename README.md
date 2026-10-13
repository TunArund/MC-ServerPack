# MC-ServerPack

面向 Minecraft Java 整合包的容器化服务端管理与远程值守工具。个人 PC 使用
`mc.sh` 创建整合包、部署和运维；`manager/` 运行 Minecraft 容器；可选的
`qqbot/` 部署到常在线 gateway，负责定时开关云主机、QQ 控制和告警。

仓库公开版本只包含程序、配置模板和空示例，不分发 Minecraft、Forge、Mod 或
玩家数据。

## 快速上手

### 1. 下载并填写基础配置

从 GitHub Releases 下载 ZIP（或克隆仓库），解压后执行：

```bash
cp .env.example .env
cp config.yml.example config.yml
chmod 600 .env
```

`.env` 只填写 gateway、QQ Bot 和 COS 密钥；所有非敏感配置都在 `config.yml`。
最小部署只需填写 `manager` 段，其中 `host` 对应个人 PC 的
`~/.ssh/config`，`dir` 是 mc-server 上的绝对部署路径。

```yaml
manager:
  region: ap-nanjing
  instance-id: ins-xxxxyyyy
  host: cvm-mc
  dir: /root/MC-ServerPack/manager
  modpack:
    max-running: 1
    overnum-action: rotate
    mem-limit: 13g
```

### 2. 创建并准备整合包

```bash
./mc.sh manager create my-pack
```

把完整服务端放入 `manager/modpacks/my-pack/src/`，再编辑 `config.yml`：

```yaml
name: 我的整合包
version: 1.0.0
java: 17
memory: 8g
port: 25565

backup:
  enabled: true
  interval: 6h
  retain: 7

dynamic-paths:
  - world/
  - logs/
  - crash-reports/
  - usercache.json
  - server.properties
```

`src/` 整体只读挂载到容器的 `/server`。`dynamic-paths` 中的路径由 `data/`
覆盖为可写并持久化；目录以 `/` 结尾，文件不加 `/`。模板已经列出 Minecraft
常见动态路径，整合包自己的缓存目录需要自行补充。

可扫描 `src/` 中名称包含 `cache` 的路径，并按输出选择要补充的条目：

```bash
./mc.sh manager config --scan my-pack
```

声明为动态路径后，首次执行 `config` 会把 `src/` 中已有内容复制到对应的
`data/` 路径；若 `data/` 已有内容则绝不覆盖。运行时由 bind mount 使用 `data/`
覆盖容器中的同名路径，不使用符号链接。

### 3. 部署 manager

```bash
./mc.sh deploy --check manager
./mc.sh deploy manager
```

部署会安装或检查 Docker、Compose、Python、cron 和 rsync，同步 manager 与
`src/`，构建 Java 8/17/21 环境镜像，生成 Compose，并按 `config.yml` 同步自动
备份及定时关服任务。部署会处理本机 `manager/modpacks/` 下的所有整合包；远端
既有的 `data/` 与 `backups/` 不会被覆盖或删除。

### 4. 启动和检查

```bash
./mc.sh manager start my-pack
./mc.sh manager status my-pack
./mc.sh manager logs my-pack
./mc.sh manager logs -f my-pack
./mc.sh manager stop my-pack
```

容器使用 `restart: unless-stopped`：正常运行的整合包会随 Docker daemon 恢复；
被人工停止的整合包不会自行启动。

### 5. 更新配置并重新同步

修改 `config.yml` 或 `src/` 后重新执行：

```bash
./mc.sh deploy manager
./mc.sh manager restart my-pack
```

### 6. 测试备份与恢复

```bash
./mc.sh manager backup my-pack
./mc.sh manager restore my-pack <backups中的文件名>
```

备份只包含 `data/world/`。回档前必须停止整合包；管理器会先备份当前世界，且不会
修改日志、缓存和其他动态文件。`config` 和部署流程会按整合包配置同步自动备份任务。

### 7. 可选：部署 gateway

需要定时开关云主机、QQ Bot 和告警时，再填写 `config.yml` 的 `gateway` 段及
`.env` 的 `GW_*`、`QQBOT_*` 密钥：

```bash
./mc.sh deploy gateway
./mc.sh bot status
./mc.sh gateway status
```

没有常在线 gateway 时，只部署 manager。个人 PC 手动启动云主机、优雅停止
Minecraft，再从云控制台关闭实例即可。

## 架构

```text
terminal（个人 PC）
  ├── 创建整合包、部署、更新、手动运维
  └── 可选：生成一次性 COS 上传/下载链接
             │
             ├───────────────┐
             ▼               ▼
gateway-server（可选）      mc-server
  ├── 7×24h QQ Bot           ├── manager/manager.sh
  ├── 定时开关 CVM           ├── Docker / Minecraft
  ├── TAT 远程调用           ├── src/ 只读内容
  └── 状态检查与告警          └── data/、backups/
```

关机分为两层：manager 先保存并停止 Minecraft；gateway 在宽限时间后请求 CVM
普通关机并复查。只有确认 Minecraft 端口已关闭时才允许 `StopType=HARD` 兜底，
否则保留实例并告警。

## 目录结构

```text
MC-ServerPack/
├── mc.sh                       # terminal 统一入口
├── .env.example                # 密钥模板
├── config.yml.example          # 非敏感配置模板
├── config.yml                  # 本机配置，不提交
├── lib/
│   └── project_config.py       # terminal 侧项目配置解析
├── cos/
│   ├── cos.py                  # 一次性 COS URL
│   └── index.html              # 玩家使用的上传页
├── manager/                    # 部署到 mc-server
│   ├── manager.sh              # 整合包配置、生命周期与备份
│   ├── config.yml              # 部署时从根配置同步，不提交
│   ├── lib/                    # 整合包配置与安全解包
│   ├── runtime/                # 容器运行时实现
│   │   ├── java*.dockerfile    # 通用 Java 环境镜像
│   │   ├── compose.yml         # 所有整合包共用的 Compose 基座
│   │   └── entrypoint.sh       # 控制台 FIFO 与优雅停止
│   └── modpacks/
│       └── template/            # 可公开分发的空模板
└── qqbot/                      # 部署到 gateway-server
```

单个整合包结构：

```text
modpacks/<ID>/
├── config.yml                  # Java、资源、端口、备份、动态路径
├── compose.yml                 # 自动生成，不手工编辑
├── src/                        # 完整服务端，只读挂载
├── data/                       # 动态路径的持久化内容
└── backups/                    # world/ 本地轮转备份
```

`lib/` 按部署边界拆分：根目录只放 terminal 使用的项目级工具，`manager/lib/`
只放会同步到 mc-server 的配置解析、状态和归档代码。QQ Bot 的运行代码继续放在
`qqbot/`。这样各部署产物自包含，也不会把无关代码同步到另一台机器。

## 高级说明

### 配置边界

- `.env`：只保存 gateway、QQ Bot 和 COS 密钥，不提交 Git。
- 根 `config.yml`：manager、gateway、COS 的非敏感配置，不提交 Git。
- `manager/config.yml`：部署时生成的远端副本，不人工维护。
- `modpacks/<ID>/config.yml`：单个整合包的 Java、内存、端口、动态路径和备份策略。
- `manager/runtime/compose.yml`：所有整合包共用的容器运行规则，项目维护。
- `modpacks/<ID>/compose.yml`：由整合包 `config.yml` 自动生成，不人工维护。

Java Dockerfile、公共 Compose 和 entrypoint 都属于容器运行时实现，因此统一放在
`manager/runtime/`；整合包目录只保留用户声明、服务端文件和运行数据。

Minecraft 容器默认使用每次执行 `manager.sh` 时由 `id -u`、`id -g` 取得的用户
身份，不假定 UID/GID 为 1000。只有显式设置 `MC_CONTAINER_UID`、
`MC_CONTAINER_GID` 时才覆盖；UID 0 会被拒绝。

`manager.instance-id` 是 gateway 调用腾讯云 API 时使用的 CVM 实例 ID；
`manager.modpack.overnum-action` 支持 `reject` 或 `rotate`。`gateway.schedule` 的
`start-elast` 是预计的实例与 Minecraft 启动耗时，因此 gateway 会在 `start`
之前这么多分钟启动 CVM；`start` 是预计可进入服务器的时间。`end-elast` 是关服后
等待实例正常关机的宽限分钟数。
`cos.prefix` 是对象键前缀，不是本地目录。

`manager.sh` 可以管理多个整合包。当前 QQ Bot 则通过部署时生成的 `MC_PACK` 固定
控制一个整合包；这是 Bot 命令与配置适配层的限制，并不是 QQ Bot 不能调用
`manager.sh`。当存在多个非 `template` 整合包时，gateway 部署会拒绝猜测目标。
若要让一个 Bot 控制多个整合包，需要后续给 Bot 命令增加整合包参数、权限检查和
目标选择，而不能只把一个配置项改成数组。

部署 gateway 时，`mc.sh` 会把 `config.yml` 中的非敏感配置、`.env` 中的密钥和
推导出的调度时间转换成远端 `qqbot/.env`。这是因为现有 Compose 与 QQ Bot 以环境
变量作为运行时接口；该文件是自动生成的部署产物，不应在 gateway 上手工维护。

`banned-ips.json`、`banned-players.json`、`ops.json` 和 `whitelist.json` 默认动态，
因为游戏内管理命令会修改它们；`server.properties` 可能被服务端补全或重写；
`eula.txt` 需要管理员首次修改。若希望其中某项严格只读，可从 `dynamic-paths`
删除并只保留 `src/` 中的文件。

### 自动备份

```yaml
backup:
  enabled: true   # 部署时安装用户 crontab；false 会移除已有任务
  interval: 6h    # 支持整除一小时的 Nm、整除一天的 Nh，或 1d
  retain: 7       # 每次成功备份后删除更旧的同整合包备份
```

`1d` 固定在每天 04:00 执行。自动备份日志位于
`data/backup.log`。修改配置后重新 `./mc.sh deploy manager` 即可更新任务。

自动上传 COS 暂不在 `config.yml` 中启用：规格会部署到 mc-server，把长期云密钥写入
其中会破坏当前权限边界。推荐后续由 gateway 使用单独的最小权限身份生成短期上传
URL，再让 mc-server 上传新备份。当前可在个人 PC 生成一次性链接：

```bash
./mc.sh cos world.tar.gz
./mc.sh cos world.tar.gz --get --expire 3600
```

### Manager 完整命令

```bash
./mc.sh manager create my-pack
./mc.sh manager remove my-pack
./mc.sh manager config my-pack
./mc.sh manager start my-pack
./mc.sh manager stop my-pack
./mc.sh manager restart my-pack
./mc.sh manager status my-pack
./mc.sh manager logs -f my-pack
./mc.sh manager backup my-pack
./mc.sh manager restore my-pack <备份文件名>
```

`remove` 默认拒绝删除含持久化数据或备份的整合包；确认不再需要时使用
`remove --force <ID>`。定时开关机和关服宽限由 gateway 负责。

### TAT 最小权限

当前 gateway 需要以下 CAM 操作：

```text
cvm:DescribeInstances
cvm:StartInstances
cvm:StopInstances
tat:RunCommand
tat:DescribeInvocationTasks
```

### 公开发布与 GitHub

发布 ZIP 或提交 GitHub 前，只保留自行编写的代码、Dockerfile、配置模板、文档和
空示例。不要提交 `.env`、Minecraft JAR、libraries、assets、未许可再分发的 Mod、
世界、玩家数据、日志、备份，或预先设置的 `eula=true`。请同时核对
[Minecraft EULA](https://www.minecraft.net/en-us/eula) 与
[Minecraft Usage Guidelines](https://www.minecraft.net/en-us/usage-guidelines)。
