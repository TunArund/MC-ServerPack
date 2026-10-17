# 整合包标准目录结构
参考./modpacks/template
```
template/
├── backups/
├── compose.yml  # 执行 config/start 后生成
├── config.yml
├── data/
└── src/
    ├── banned-ips.json
    ├── banned-players.json
    ├── config/
    ├── crash-reports/
    ├── eula.txt
    ├── logs/
    ├── ops.json
    ├── server.properties
    ├── usercache.json
    ├── user_jvm_args.txt # 个性化java启动参数
    ├── usernamecache.json
    ├── whitelist.json
    └── world/ # 重要：你的存档
```
|目录|作用|
----|----
src/        | 存放整合包实际包含的文件
config.yml  | 配置文件
data/       | 存放运行时动态修改的文件和目录，由persistent指定
backups/    | 存放备份文件，一般是persistent指定路径的压缩包
compose.yml | 由config.yml自动生成，指导docker容器创建

# Manager 命令

`compose.yml` 是生成物，不要手工修改。将服务端文件放入 `src/`，并根据实际
启动脚本修改 `config.yml` 中的 `command`。

Manager 独立读取并持有自己的 `config.yml`，不依赖 Terminal 或 Monitor 的运行时文件。
通过 Terminal 部署时，只提取 Terminal 配置中的 `manager` 段初始化该文件；手工独立部署时执行：

```bash
cp config.yml.example config.yml
./manager.sh check
./manager.sh install docker
./manager.sh install
./manager.sh create example
./manager.sh create vanilla --vanilla
./manager.sh create vanilla-locked --vanilla 1.21.10 --accept-eula
./manager.sh config example
./manager.sh start example
./manager.sh status example
./manager.sh logs example --lines 100
./manager.sh logs example --follow
./manager.sh console example say hello
./manager.sh backup example
./manager.sh stop example
./manager.sh restore example
./manager.sh remove example --force
./manager.sh stop-all
./manager.sh export example
./manager.sh import example /path/to/example.tar.gz
./manager.sh schedule install
./manager.sh schedule run
./manager.sh schedule remove
```

`check` 只验证 Python 3.9+、venv、Docker Engine、Compose 插件和 crontab，不会安装或修改系统软件。
`install docker` 在 Debian/Ubuntu 上配置 Docker 官方 APT 仓库，安装 Engine、Buildx
和 Compose 插件，启用服务并将当前非 root 用户加入 `docker` 组。该组具有等同 root
的 Docker 权限；安装完成后需重新登录，再执行 `./manager.sh check`。
`install` 在环境检查通过后安装 Manager 自身 Python 依赖并安装聚合调度任务。
`create --vanilla [version]` 从 Mojang 官方元数据下载原版服务端，校验文件大小与
SHA-1，并自动写入版本、Java 镜像和启动命令。不指定版本时使用最新正式版；默认
`eula=false`，只有显式添加 `--accept-eula` 才写入 `eula=true`。
 `export/import` 导入导出整合包完整结构，含src,data,config


`start` 会按 Java 版本与运行时文件内容哈希构建镜像，并在容器健康检查通过后返回。
修改 Dockerfile 或 `entrypoint.sh` 会自动得到新镜像标签，不会静默复用旧运行时。

# 定时任务cronjob

只会生成一个 Manager 用户级 cron，不要为每个整合包分别创建 cronjob：

```bash
./manager.sh schedule install
```

每分钟运行一次聚合检查，并在 `.schedule-state.json` 上加锁阻止任务重叠；该文件也保存
当天已完成的调度动作。`schedule install` 是幂等的，重复执行无影响。
有 `# mc-serverpack-manager-schedule` 标记，不会误改其他 cronjob。
## 定时备份
每个整合包在自己的 `modpacks/<id>/config.yml` 中独立决定是否备份、备份周期和保留数量：

整合包 `config.yml` 的 `backup.enabled` 为 `true` 时，`interval` 决定备份间隔，
支持分钟、小时和天，例如 `30m`、`6h`、`1d`。`retain` 控制最大保留份数。

```yaml
backup:
  enabled: true
  interval: 6h
  retain: 7
  quiesce-seconds: 5
```

`interval` 是相对周期，不是每天的固定时刻。开启自动备份但尚无任何备份的整合包，
会在下一次聚合检查时立即建立首份备份；某个整合包失败不会阻止其他包继续备份。

## 定时关服与关机

Manager 本机 `config.yml` 的 `modpack` 配置整合包停服，顶层 `auto-stop` 配置云主机关机。
以下配置会按 60、30、15、7、3、1
分钟逐步广播，在 23:00 优雅停止所有整合包，并在 23:05 请求云主机关机：

```yaml
auto-stop: true
stop-at: '23:05'
power-api:
  provider: tencent
  instance-id: ins-xxxxyyyy
  instance-region: ap-nanjing
modpack:
  auto-stop: true
  stop-at: '23:00'
  stop-notify-elast: 60
  stop-notify-step: 2
```

`stop-notify-elast` 是第一次提前通知的分钟数，后续通知按 `stop-notify-step` 做整数除法，直到
1 分钟。停服失败时会在云主机关机时间前每分钟重试；云 API 调用失败后会在 5 分钟内重试。
成功动作写入 `.schedule-state.json`，同一天不会重复执行。进入停服窗口后不会再开始
新的备份。

`power-api.provider: tencent` 使用内置的腾讯云 SDK 适配；例如
`provider: xxx_aaa` 会自动寻找 `lib/providers/xxx_aaa.py`。Provider ID 仅允许
大小写字母、数字和下划线。该文件必须实现
`status`、`start`、`stop`，URL 和厂商细节写在文件内。先安装依赖并填写密钥文件：

```bash
python3 -m venv .venv
./.venv/bin/pip install -r lib/requirements.txt
cp .env.example .env
```

腾讯云从 `.env` 读取 `POWER_SECRET_ID` 与 `POWER_SECRET_KEY`；示例 HTTP provider 将
`POWER_SECRET_KEY` 作为 Bearer Token，只有一个 Token 时 `POWER_SECRET_ID` 留空。
该文件不会进入 Git。自定义转接口的请求与状态响应契约见项目
`readme.md` 的“电源 API”。
调用前会先查询实例状态；已停止或正在停止时视为成功，运行中实例使用软关机并选择
关机不收费模式（腾讯云）。建议云子账号只授予目标实例查询和关机所需的最小权限。

修改配置后可先手工执行 `./manager.sh schedule run` 验证。它不会提前触发尚未到期的
停服动作；确认无误后再运行 `schedule install`。`schedule remove` 只移除这一条聚合任务。
