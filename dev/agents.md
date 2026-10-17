- 基准
当前仅个人使用，随时破坏性变更。不会因兼容性妥协
保持简洁，不随意创建源码文件，不留空的`__init__.py`
- 测试
/tests目录
```bash
python3 -m unittest discover -s tests
```
# 目录结构
/lib/{cli.py,config.py,deploy.py,manager.py,monitor.py,power.py,remote.py,providers/}
/lib/providers/{tencent.py,example.py}

/mcsp.{sh,exe}
/{config.yml,.env,README.md}
配置、密钥、说明

/manager/{readme.md,manager.sh,lib/,runtime/,modpacks/}
lib/{backup.py,cli.py,config.py,docker.py,modpacks.py,transfer.py,power.py,schedule.py,providers/}
lib/providers/{tencent.py,example.py}
runtime/{java{8,17,21}.dockerfile,entrypoint.sh}
modpacks/template/{src/,data/,backups/,config.yml}

/monitor/{src/,data/,Dockerfile,compose.yml,monitor.sh}
src/{app.py,config.py,monitor/,qqbot/,requirements.txt}
src/monitor/{service.py,power.py,schedule.py,providers/}
后续可能改go实现

/dev存放开发工具，无需关注
## monitor
Monitor 保留 `monitor.manager.auto-start/start-at/auto-stop/stop-at` 配置。
每 30 秒检查一次云主机状态；每天的开、关机事件成功处理后写入
`monitor/data/state.json`，失败会在下一周期重试。进程重启会补处理当前时间线上
最近的事件。

生产镜像会复制 `monitor/src`，运行时只挂载只读 `config.yml` 和可写 `data/`；
不挂载源码，以保证同一镜像的行为可复现。

## 电源 API

Manager 和 Monitor 各自通过 `power-api` 配置目标实例。`provider` 是适配器 ID，
仅允许大小写字母、数字和下划线。例如 `provider: xxx_aaa` 会自动寻找
`providers/xxx_aaa.py`；`tencent` 是内置的腾讯云适配器：

```yaml
power-api:
  provider: xxxaaa
  instance-id: minecraft-manager
  instance-region: local
```

自定义 provider 必须实现 `status`、`start`、`stop` 三个函数，每个函数的签名为
`(instance_id, region, secret_id, secret_key) -> dict`。加载时会检查文件、必要函数和参数签名

# 测试环境
## 本机
- Windows 10 宿主机：后续测试 Windows 发行版。
- Ubuntu 虚拟机：后续测试 Linux 发行版；本轮只测试源码用户体验 `./mcsp.sh`，暂缓实现和测试 release。
- 2026-10-17 盘点：Ubuntu 22.04、Python 3.10.12、Git 2.34.1、OpenSSH 8.9p1。

## Monitor
- 腾讯云 Lighthouse 包年包月 Ubuntu 22.04 实例。
- SSH：`steam@l4d2.tunarund.top`，已配置免密登录和免密 sudo。
- 已安装 Docker。
- 2026-10-17 盘点：Docker 26.1.3、Compose 2.27.1，根分区剩余 23 GB，内存约 1.4 GiB 可用；部署目标目录 `/home/steam/MC-ServerPack-monitor` 尚不存在。
- 已有 8 个 L4D2 相关容器；新 Monitor 不应占用现有的 80、443、27014、27015 等主机端口。
- 旧 QQ Bot 位于 `/home/steam/cvm-power`，由系统级 `mc-qqbot-menu.service` 以 `steam` 用户运行，不是由 cron 直接启动；2026-10-17 状态为 active/running。
- `steam` 用户 cron 在 19:50 开机、23:00 关机、每 30 分钟执行 guard；另有 ACME cron。迁移到新 Monitor 前必须先避免两套电源日程并行。
- 旧实现已有 `.cancel-tonight`、`.alert-snooze`、`.alert-state` 等状态文件，可作为“取消预订”和告警静默功能的行为参考，但不能读取或迁移其中的密钥配置。

## Manager
- 腾讯云 CVM，2 核 16 GB，按量计费 Debian 实例，随用随开。
- SSH：`root@mc.tunarund.top`，已配置免密登录；目前只有 root 用户。
- 尚未安装 Docker。
- `/root/closing-song` 正在运行大型 Minecraft 整合包，由 systemd 设置开机自启。
- 现有 cronjob 可能负责定时备份；部署或迁移前必须先只读检查 systemd、cron、端口、磁盘空间和备份目录，不得影响现有实例。
- 2026-10-17 盘点时主机 22 端口连接超时，推定实例处于关机或未开放状态；未主动开机，其他项目待开机后检查。

# 测试原则
- 先在 Ubuntu 虚拟机验证 Terminal 源码安装、配置检查、帮助信息和错误提示。
- 远端先执行只读盘点和 `deploy manager|monitor --test`；没有明确迁移方案前，不在生产 Manager 上执行部署、安装 Docker、停服或电源操作。
- Monitor 首次部署前先停用或隔离旧 QQ Bot，避免同一机器人重复消费事件。
- 发行版测试留到 `release.py` 实现之后。

# TODO
- monitor qqbot
重排src目录与文件
app.py config.py network.py power.py
schedule.py( orchestra power qqbot by state machine ) qqbot.py(recv_msg send_msg qq relate stuff) message.yml(msg template)
qqbot实现 取消预订 告警时可忽略1/2/5h
参照添加docker安装
manager/lib/docker.py#DockerInstaller
- 更新*.md /test/
- 实现release.py
release.py 构建发行版（未实现）
将`/**/lib` python运行环境打包，生成`*.sh`的二进制
然后将manager monitor env config readme打包压缩，发布Linux和Windows版
二期
- terminal mcsp回显规范化
```
./mcsp.sh deploy --test monitor
错误：manager.user 必须是非空字符串
```
未显示执行步骤与报错来源
```
加载config.yml
加载config.yml错误：xxx
```

```
steam@ubuntu:~/Minecraft/MC-ServerPack$ ./mcsp.sh deploy monitor --test
部署检查通过：monitor
```
未显示执行步骤
```
ssh连接 ssh username@hostaddr
检查xxx
检查yyy
```
```
steam@ubuntu:~/Minecraft/MC-ServerPack$ ./mcsp.sh start manager
Manager 云主机：RUNNING，公网 IP=119.45.115.65
```
未显示执行步骤，等待40s后才打印一行
- 扁平化config.yml
去掉.monitor.manager的power-api
只填.monitor.manager
去掉.manager.power-api

- 实现cos备份整合包或存档
