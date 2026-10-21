# 发行设计

本文记录 MC-ServerPack 的共享源码同步、独立组件打包、跨平台 Terminal 和捆绑发行方案。
构建入口已经实现，并由 `.github/workflows/release.yml` 在 Linux 与 Windows 原生 runner
上构建。本文同时保留后续本地 `modpack` 领域抽取的实施边界；尚未实现的命令不能仅凭
出现在本文中就视为可用功能。

## 目录边界

所有发行相关源码、设计、暂存文件和产物统一放在 `dev/release/`，不得再散落到 `dev/`
根目录：

```text
dev/release/
├── readme.md       发行设计
├── release.py      构建入口
├── src/            后续需要拆分时使用的构建辅助模块
├── build/          构建暂存，Git 忽略
└── dist/           最终发行产物，Git 忽略
```

测试仍统一放在 `dev/tests/`；Release 专用 fixture 如有必要放在
`dev/release/fixtures/`，但不得混入真实配置、密钥或运行数据。
GitHub 要求工作流入口位于 `.github/workflows/`，因此该目录中的 `release.yml` 是唯一位置
例外；它只能编排并调用 `dev/release/release.py`，不得复制构建业务逻辑。

## 目标产物

一次正式发行生成三份独立组件包和一份捆绑包。Terminal 按宿主平台分别构建，Manager
与 Monitor 始终部署到 Linux Docker 主机，其源码包与操作 Terminal 的平台无关。

```text
dev/release/dist/<version>/
├── mc-serverpack-terminal-linux-x64.tar.gz
├── mc-serverpack-terminal-windows-x64.zip
├── mc-serverpack-manager.tar.gz
├── mc-serverpack-monitor.tar.gz
├── mc-serverpack-bundle-linux-x64.tar.gz
├── mc-serverpack-bundle-windows-x64.zip
├── manifest.json
└── checksums.sha256
```

捆绑包不是第四套源码。它组合相同版本的 Terminal 可执行文件以及 Manager、Monitor 两个
payload，方便 Terminal 直接部署：

```text
MC-ServerPack/
├── mcsp[.exe]
├── config.yml.example
├── .env.example
├── logs/
├── modpacks/
└── payloads/
    ├── manager/        可直接同步到远端的 Manager 发行树
    └── monitor/        可直接同步到远端的 Monitor 发行树
```

PyInstaller 单文件模式只封装 Terminal 自身；捆绑包在可执行文件旁保留展开后的部署资源，
避免运行时解压到系统临时目录，也避免把 `_MEIPASS` 误当作用户数据目录。

## 本地整合包约定

`./modpacks` 始终相对于所属组件根目录，而不是仓库根目录或调用命令时的当前目录。
命令不接受任意路径参数：

```text
terminal/modpacks/<id>/       Terminal 本地可编辑工作区
terminal/modpacks/<id>.tgz    Terminal 本地标准归档
manager/modpacks/<id>/        Manager 远端工作区
manager/modpacks/<id>.tgz     Manager 远端标准归档
```

本地命令固定为：

```text
mcsp modpack create <id>   从模板创建 terminal/modpacks/<id>/
mcsp modpack remove <id>   删除本地工作区，默认要求确认
mcsp modpack list          列出本地工作区
mcsp modpack check <id>    校验并显示整合包详情
mcsp modpack export <id>   terminal/modpacks/<id>/ -> terminal/modpacks/<id>.tgz
mcsp modpack import <id>   terminal/modpacks/<id>.tgz -> terminal/modpacks/<id>/
mcsp deploy modpack <id>   上传 terminal/modpacks/<id>.tgz
```

最小部署流程是 `create -> export -> deploy modpack`。`export` 必须先执行与 `check` 相同的
校验；`deploy` 只消费已经生成的归档，不隐式重新打包。`manager` 命令空间继续透明转发
远端 Manager API，不与本地 `modpack` 命令合并。

整合包格式、配置解析、工作区操作、manifest、哈希和安全归档逻辑以 `lib/modpack_sdk.py` 为
源码真源，发行前同步到 Terminal 与 Manager。Docker、Job、远端状态、运行数据和备份
生命周期仍只属于 Manager。

## 共享源码同步

`lib/` 是共享源码真源，运行时不得跨组件导入仓库根目录。`release.py sync` 将下列文件
复制到需要它们的组件，`release.py check` 和单元测试检查副本字节一致：

```text
config_sdk.py
docker_sdk.py
docker-install.sh -> terminal/src/docker-install.sh
manager_sdk.py
observability_sdk.py
power_sdk.py
providers/*.py
modpack_sdk.py
```

共享文件不允许从组件副本反向覆盖 `/lib`。同步后若工作树出现变化，开发者应检查并提交，
避免发行过程悄悄使用未提交源码。

## release.py 命令

入口放在 `dev/release/release.py`：

```text
python dev/release/release.py sync
python dev/release/release.py check
python dev/release/release.py build terminal
python dev/release/release.py build manager
python dev/release/release.py build monitor
python dev/release/release.py build bundle
python dev/release/release.py build all
python dev/release/release.py verify
python dev/release/release.py checksums
```

- `sync`：同步共享源码，不构建产物。
- `check`：检查 Git 版本、共享副本、组件清单、禁止的跨组件导入和必要资源。
- `build`：在 `dev/release/build/` 暂存，在 `dev/release/dist/<version>/` 原子生成产物。
- `verify`：解压到系统临时目录并从发行内容运行检查，不从源码树补文件。
- `checksums`：为最终产物生成 SHA-256，并写包含版本、commit、平台和文件清单的 manifest。

默认拒绝从脏工作树构建正式版本；本地调试可显式使用 `--allow-dirty`，manifest 必须标记
`dirty: true`。版本由显式 `--version` 或 Git tag 提供，不能从当前日期隐式生成。

## 组件清单

Terminal 包含可执行文件、配置示例、空日志/整合包目录、Docker 安装脚本、本地整合包
模板及许可证。不得包含真实 `config.yml`、`.env`、日志、缓存、测试或用户整合包。

Manager 包含 `manager.sh`、`src/`、`runtime/`、`modpacks/template/`、配置示例及空数据目录；
Monitor 包含 `monitor.sh`、`src/`、Dockerfile、Compose、配置示例及空数据目录。两者均不得
包含密钥、TLS 私钥、Job、传输缓存、QQ 目标、备份或运行日志。

## Windows Terminal

Windows 只运行 Terminal，Manager 与 Monitor 仍部署到 Linux。Windows 可执行文件由
Windows runner 使用 PyInstaller 构建，不从 Linux 交叉编译。运行时不能依赖 Bash、WSL、
本机 Docker、系统 Python、`ssh` 或 `scp`；SSH/SFTP 使用 Paramiko。

构建必须显式收集动态 Provider、`docker-install.sh`、配置模板、modpack 模板和 `tzdata`。
所有路径使用 `pathlib`；临时目录使用 `tempfile`；不得依赖 POSIX chmod、符号链接或
`fcntl`。Windows 包以 ZIP 分发，避免解压工具错误处理可执行权限。

`mcsp local manager` 已删除；本地文件操作统一使用
`modpack create/remove/list/check/export/import`。
Windows 与 Linux Terminal 使用相同 Python 领域逻辑，不维护 Windows 专用命令分支。

## CI 与验收

Ubuntu runner 构建 Manager、Monitor、Linux Terminal 和 Linux Bundle；Windows runner
构建 Windows Terminal。最终汇总 Job 只组合已经校验且 commit 相同的产物。

每个平台至少验证：

```text
mcsp[.exe] --help
mcsp[.exe] config init
mcsp[.exe] --json config check
mcsp[.exe] modpack create release-test
mcsp[.exe] modpack check release-test
mcsp[.exe] modpack export release-test
mcsp[.exe] modpack remove release-test --force
mcsp[.exe] modpack import release-test
```

还必须验证三个独立包不读取源码树外文件、捆绑包中的 payload 哈希与独立包一致、归档不含
密钥与运行数据、shell 入口通过语法检查、Python 模块可编译、完整单元测试通过。

## 实施状态

发行前架构项已经完成：共享整合包领域层、本地 `modpack` 六个命令、只消费标准归档的
`deploy modpack <id>`、移除 `local manager`、共享 Docker 命令与镜像部署原语、共享源码
同步检查、跨平台 PyInstaller 构建、manifest、checksum 与 CI 发布流程。
