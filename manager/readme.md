# 用户账户
## 账号创建
useradd username
usermod -aG sudo,docker username
echo 'username ALL=(ALL:ALL) NOPASS | sudo tee /etc/sudoers.d/username'

## ssh免密登录
ssh-keygen

~username/.ssh/authorized_keys 写入公钥，如id_ed25519.pub的内容

Done，如出错或不知如何进行，自行咨询D老师

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
    ├── user_jvm_args.txt
    ├── usernamecache.json
    ├── whitelist.json
    └── world/
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

Manager 的 `config.yml` 由 Terminal 从根 `config.yml` 的 `manager` 子树原样生成。
日常修改应先改根配置，不要分别维护两份结构。仅在脱离 Terminal 手工部署时才执行：

```bash
cp config.yml.example config.yml
./manager.sh create example
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
./manager.sh schedule install
./manager.sh schedule run
./manager.sh schedule remove
```

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

根 `config.yml` 的 `manager.schedule` 可配置一次全局每日停服，部署时会随 `manager`
子树原样生成到 Manager。以下配置会按 60、30、15、7、3、1
分钟逐步广播，在 23:00 优雅停止所有正在运行的整合包，并在成功后请求云主机关机：

```yaml
schedule:
  auto-stop: true
  stop-at: '23:00'
  notify-elast: 60
  notify-step: 2
  poweroff:
    enabled: true
    deadline: 5
    region: ap-nanjing
    instance-id: ins-xxxxyyyy
```

`notify-elast` 是第一次提前通知的分钟数，后续通知按 `notify-step` 做整数除法，直到
1 分钟。停服或云 API 调用失败时，聚合任务会在 `deadline` 分钟窗口内每分钟重试；
成功动作写入 `.schedule-state.json`，同一天不会重复执行。进入停服窗口后不会再开始
新的备份。

云关机使用腾讯云 `tccli`。先安装依赖并填写密钥文件：

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

`.env` 中只需要 `POWER_SECRET_ID` 与 `POWER_SECRET_KEY`，该文件不会进入 Git。
调用前会先查询实例状态；已停止或正在停止时视为成功，运行中实例使用软关机并选择
关机不收费模式。建议云子账号只授予目标实例查询和关机所需的最小权限。

修改配置后可先手工执行 `./manager.sh schedule run` 验证。它不会提前触发尚未到期的
停服动作；确认无误后再运行 `schedule install`。`schedule remove` 只移除这一条聚合任务。
