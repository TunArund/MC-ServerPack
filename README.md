# MC-ServerPack

面向 Minecraft Java 整合包的容器化服务端管理工具。个人 PC 负责部署与操作，
manager 在 Minecraft 服务器运行容器；可选的 gateway 负责定时开关云主机、QQ Bot
和告警。

## 快速上手

先选择入口：Windows 发行包使用 `mc.exe`；Linux 发行包使用 `./mc.sh`；源码用户
先执行 `uv venv && uv pip install --python .venv -r lib/requirements.txt`。Linux
独立程序受 glibc 兼容范围影响，较老或特殊发行版可改用源码和项目内 `.venv`。

复制配置模板，在 `config.yml` 中填写 manager 的 `user`、`host` 和 `dir`：

```bash
cp .env.example .env
cp config.yml.example config.yml
./mc.sh deploy manager
./mc.sh modpack create my-pack
./mc.sh modpack import my-pack /path/to/server.zip
./mc.sh modpack deploy my-pack
./mc.sh manager start my-pack
./mc.sh manager status my-pack
```

Windows 将示例中的 `./mc.sh` 替换成 `mc.exe`。
需要定时开关机和 QQ Bot 时，再填写gateway 配置并执行 `./mc.sh deploy gateway`。

平台发行包已把 terminal 代码编入 `mc`/`mc.exe`，因此不附带根 `lib/`；用于部署
mc-server 的 `manager/lib/` 仍会保留。

## 架构

```text
terminal（个人 PC）
  ├── mc.sh / mc.exe：配置、部署、整合包管理
  ├───────────────┐
  ▼               ▼
gateway（可选）   mc-server
  ├── QQ Bot       ├── manager.sh
  ├── 定时开关机    ├── Docker / Minecraft
  └── 告警         └── 整合包数据与备份
```

manager 可以管理多个整合包。gateway 当前固定控制一个整合包：关机时先让 manager
保存并停止 Minecraft，宽限时间后再关闭云主机并检查结果。

## 目录结构

```text
MC-ServerPack/
├── mc.sh                       # Linux/源码入口
├── config.yml.example          # 项目配置模板
├── .env.example                # 密钥模板
├── index.html                  # COS 上传页
├── lib/                        # terminal Python 核心
├── dev/                        # 开发与发布工具
├── manager/
│   ├── manager.sh              # 远端整合包管理入口
│   ├── lib/                    # 配置、状态和归档逻辑
│   ├── runtime/                # Java 镜像、Compose、entrypoint
│   └── modpacks/template/      # 空整合包模板
└── qqbot/                      # gateway 部署产物
```

单个整合包：

```text
manager/modpacks/<ID>/
├── config.yml                  # Java、内存、端口、动态路径、备份
├── compose.yml                 # 自动生成
├── src/                        # 服务端静态文件，只读挂载
├── data/                       # 动态数据
└── backups/                    # world/ 轮转备份
```

## 配置

根 `config.yml` 描述部署目标：

```yaml
manager:
  user: steam
  host: 10.0.0.5
  dir: /home/steam/MC-ServerPack/manager
```

`manager.modpack` 可设置同时运行数量、超限策略、全局内存限制和启停超时。gateway
和 COS 均为可选模块；对应密钥放在 `.env`。

发行包包含 `manager/lib/` 的 Python 源码，部署时会随 `manager.sh` 一起同步到
mc-server，因此不受 terminal 发行包平台影响。manager 需要 Python 3.8+、PyYAML、
Docker Compose 和 cron。Debian/Ubuntu 可由部署流程自动安装；其他 Linux 发行版
预先安装这些依赖后也可以运行。

整合包的 `config.yml` 示例：

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

dynamic-paths:
  - world/
  - logs/
  - crash-reports/
  - server.properties
```

`src/` 整体只读挂载；`dynamic-paths` 由 `data/` 覆盖为可写持久化路径。目录以 `/`
结尾，文件不加 `/`。首次配置会从 `src/` 初始化尚不存在的动态内容，不覆盖已有
`data/`。`modpack scan <ID>` 可查找名称包含 `cache` 的候选路径。

备份只包含 `data/world/`。恢复前必须停止整合包，恢复时会先保存当前世界。自动备份
由用户级 crontab 执行。

gateway 的 `start-elast` 表示为启动云主机和 Minecraft 预留的时间；`end-elast`
表示关服后的正常关机宽限时间。腾讯云 CAM 最小权限为：

```text
cvm:DescribeInstances
cvm:StartInstances
cvm:StopInstances
tat:RunCommand
tat:DescribeInvocationTasks
```

## 指令

```text
deploy  manager|gateway|all [--check]       部署或检查环境
modpack create <ID>                         创建本地整合包
modpack import [--force] <ID> <文件或URL>   导入 JAR、ZIP 或 TAR
modpack scan <ID>                           扫描 cache 路径
modpack deploy <ID>                         上传指定整合包

manager remove|config|start|stop|restart    远程管理整合包
manager status|logs|backup|restore          状态、日志与存档
gateway start|stop|status|guard             云主机电源控制
gateway config                              验证 gateway 配置
bot status|restart|stop|logs|follow         QQ Bot 管理
cos <对象名> [--get]                        生成 COS 临时链接
tui                                         进入 mc> 交互模式
```

不带参数启动也会进入 TUI。在 `mc>` 输入模块名可进入该模块，例如输入 `bot` 后在
`mc bot>` 直接使用 `status`、`logs`；输入 `back` 返回根层。

`modpack` 在个人 PC 执行，`manager` 通过 SSH 调用 mc-server 上的 `manager.sh`。
修改整合包后重新 `modpack deploy <ID>`，再执行 `manager restart <ID>`。
