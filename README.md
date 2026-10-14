# MC-ServerPack

面向 Minecraft Java 整合包的容器化服务端管理工具。个人 PC 负责配置和部署，
manager 在 Minecraft 服务器运行容器；可选 gateway 提供定时开关机、QQ Bot 和告警。

## 快速上手

Windows 发行包运行 `mc.exe`，Linux 发行包运行 `./mc.sh`。启动后输入：

```text
mc> quickstart
```

向导会依次完成：

```text
配置连接 → 隐藏输入必要密钥 → 部署 manager → 导入整合包 → 上传 → 启动并检查
```

每一步都会先显示等价的 `mc ...` 命令，部署失败时可以根据提示重试或退出。完成
向导后，可以继续在 TUI 输入 `manager`、`modpack`、`gateway`、`bot` 进入对应模块。

源码用户先准备隔离环境：

```bash
uv venv
uv pip install --python .venv -r lib/requirements.txt
./mc.sh
```

Linux 独立程序受 glibc 兼容范围影响，较老或特殊发行版可使用源码模式。

## 架构

```text
terminal（个人 PC）
  ├── mc / mc.exe：向导、部署、运维
  ├───────────────┐
  ▼               ▼
gateway（可选）   mc-server
  ├── QQ Bot       ├── manager.sh
  ├── 定时开关机    ├── Docker / Minecraft
  └── 告警         └── 整合包数据与备份
```

manager 支持多个整合包；gateway 当前固定控制一个整合包。平台发行包已将 terminal
代码编入 `mc`/`mc.exe`，但会保留部署到 mc-server 的 `manager/lib/`。

## 目录

```text
MC-ServerPack/
├── mc.sh                       # Linux/源码入口
├── config.yml.example          # 部署配置模板
├── .env.example                # 密钥模板
├── index.html                  # COS 上传页
├── lib/                        # terminal Python 核心
├── dev/                        # 开发与发布工具
├── manager/                    # mc-server 管理端
└── qqbot/                      # gateway 部署产物
```

- [manager 配置、整合包目录、动态挂载、备份与命令](manager/README.md)
- [gateway、QQ Bot、定时开关机与 CAM 权限](qqbot/README.md)
- [空整合包模板和 Forge 准备方式](manager/modpacks/template/README.md)

## 配置文件

- `config.yml`：manager、gateway 和 COS 的非敏感配置。
- `.env`：云 API、QQ Bot、COS 等密钥。
- `manager/modpacks/<ID>/config.yml`：单个整合包的 Java、内存、端口和持久化策略。

上述本地配置和运行数据均不会被 Git 追踪。向导只询问已启用模块所需的密钥，且
安全输入不会显示在终端。

## 指令

```text
quickstart                                  首次配置向导
deploy manager|gateway|all [--check]        部署或检查环境
modpack create|import|scan|deploy           创建、导入目录/压缩包并上传整合包
manager remove|config|start|stop|restart    远程生命周期管理
manager status|logs|backup|restore          状态、日志与存档
gateway config|start|stop|status|guard      gateway 与云主机
bot status|restart|stop|logs|follow         QQ Bot
cos <对象名> [--get]                        COS 临时链接
tui                                         交互模式
```

不带参数启动即进入 TUI。输入模块名后提示符会变成 `mc manager>` 等形式；输入
`back` 返回根层，`help` 查看当前层命令。
