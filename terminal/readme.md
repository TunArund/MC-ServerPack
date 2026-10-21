# Terminal

Terminal 是 MC-ServerPack 的本地控制端。可从Windows 或 Linux 准备远端服务器、部署 Manager/Monitor、上传整合包并完成日常管理

## 下载与运行

从 [GitHub Releases](https://github.com/TunArund/MC-ServerPack/releases) 下载对应平台的
`mcsp-windows-x64.zip` 或 `mcsp-linux-x64.tar.gz`，完整解压到固定目录。不要只取出
启动器：稳定运行库位于 `lib/`，Terminal 与远端部署程序位于 `data/`。

后续普通程序更新只需下载 `mcsp-data.zip` 并整体替换 `data/`；根目录的 `config.yml`、
`.env`、`logs/`、`state/` 和 `modpacks/` 不会被覆盖。

- Linux：在解压目录中使用 `./mcsp`。
- Windows PowerShell：使用 `.\mcsp.exe`；下文的 `./mcsp` 均替换为该命令。

## 快速上手

```bash
./mcsp config init
# 根据“附录：配置与密钥”填写 .env 与 config.yml

./mcsp modpack create amodpack # 请随意替换此名称
# 根据“附录：整合包”把服务端文件放入 modpacks/amodpack/src/，并调整 config.yml

./mcsp deploy manager --create-user  # 若已有免密 sudo 普通用户可跳过
./mcsp deploy manager
./mcsp deploy modpack amodpack

./mcsp manager start amodpack
```

`deploy manager` 会检查SSH连接、按需准备 Docker、增量部署 Manager，并同步配置与密钥。
首次部署还会记录 Manager API 密钥及私有 TLS CA。某一步失败时，修正提示的问题后重试
同一命令即可。

部署前后可主动检查：

```bash
./mcsp config check
./mcsp deploy manager --test
./mcsp manager status amodpack
```

## 常用命令

本地整合包：

```bash
./mcsp modpack create <id>
./mcsp modpack list
./mcsp modpack check <id>
./mcsp modpack export <id>
./mcsp modpack import <id>
./mcsp modpack remove <id>
./mcsp deploy modpack <id>
```

远端整合包：

```bash
./mcsp manager list
./mcsp manager status <id>
./mcsp manager start <id>
./mcsp manager stop <id>
./mcsp manager enable <id> --now
./mcsp manager disable <id> --now
./mcsp manager logs <id> --lines 100
./mcsp manager console <id> say hello
./mcsp manager backup <id>
```

Manager 服务器与可选 Monitor：

```bash
./mcsp status manager
./mcsp start manager
./mcsp stop manager

./mcsp deploy monitor --create-user
./mcsp deploy monitor
./mcsp status monitor
./mcsp monitor add-admin
```

`stop manager` 会先安全停止全部整合包，再关闭服务器。完整命令分别见
[Manager 手册](../manager/readme.md)和 [Monitor 手册](../monitor/readme.md)。

## 输出选项

全局选项放在模块名前：

```bash
./mcsp --quiet deploy manager
./mcsp --json manager list
```

默认最终结果写 stdout，进度和错误写 stderr。`--quiet` 隐藏进度与成功提示；`--json`
保证 stdout 只包含一个 JSON 文档，两者互斥。Terminal 的轮转日志位于
`logs/terminal.log`。

## 附录：配置与密钥

`./mcsp config init` 会从发行包模板创建 `config.yml` 和 `.env`，不会覆盖已有文件。

`config.yml` 至少需要确认：

- `manager.host`：Manager 主机域名或 IP；
- `manager.user`：已有免密 sudo 普通用户，或准备通过 `--create-user` 创建的用户名；
- `manager.dir`：远端独立部署目录；
- `manager.api.port`：端口是否允许入站（安全组）
- `manager.timezone`：IANA 时区，例如 `Asia/Shanghai`；
- `manager.power-api.instance-id` 与 `instance-region`：腾讯云 CVM 实例 ID 和地域；使用定时关机时填写
- `manager.modpack.mem-limit`：全部同时运行整合包可使用的内存上限。

`.env` 中常用字段：

```dotenv
POWER_SECRET_ID=
POWER_SECRET_KEY=
MANAGER_API_KEY=
QQBOT_APP_ID=
QQBOT_APP_KEY=
COS_SECRET_ID=
COS_SECRET_KEY=
```

- `POWER_SECRET_ID`/`POWER_SECRET_KEY` 用于腾讯云开关机；创建编程子用户和授权方式见
  [根 README：腾讯云 API 密钥获取](../readme.md#腾讯云-api-密钥获取)。
- `MANAGER_API_KEY` 留空即可，首次部署 Manager 时自动生成并回填。
- QQ Bot 与 COS 未启用时，对应字段保持为空。
- `.env` 含明文密钥，不要发送给他人或提交到 Git。

编辑完成后运行 `./mcsp config check`。
若不需要 Monitor，保持`monitor.enabled: false`。

## 附录：整合包

本地工作区固定为：

```text
modpacks/
├── template/
└── amodpack/
    ├── config.yml
    └── src/
```

执行 `./mcsp modpack create amodpack` 后：

1. 将服务端 JAR、模组、配置等完整服务端文件放入 `modpacks/amodpack/src/`；
2. 编辑 `modpacks/amodpack/config.yml`，设置 Java、内存、端口和备份；
3. 阅读 Minecraft EULA 后将 `eula` 改为 `true`；
4. 运行 `check` 检查详情，再执行 `export` 和 `deploy`。

示例：

```yaml
name: 示例整合包
java: 17
mem-limit: 8g
port: 25565
eula: true
version: 1.0.0
backup:
  enabled: true
  interval: 6h
  retain: 7
  quiesce-seconds: 5
read-only:
  enabled: false
  persistent: []
```

`export <id>` 生成 `modpacks/<id>.tgz`；`deploy modpack <id>` 只上传这个归档，不会
隐式重新打包。因此修改 `src/` 或配置后，需要重新执行 `check`、`export`、`deploy`。

## 源码运行

仅源码用户需要 Python 环境：

```bash
python3 -m pip install -r terminal/requirements.txt
./terminal/mcsp.sh --help
```

Terminal 在运行时不会导入仓库根目录的 `lib/`；发行同步后的共享 SDK 副本位于
`terminal/src/`，因此发行包可以完全独立运行。
