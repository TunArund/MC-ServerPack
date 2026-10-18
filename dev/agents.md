- 基准
当前仅个人使用，随时破坏性变更。不会因兼容性妥协
保持简洁，不随意创建源码文件，不留空的`__init__.py`
- 测试
/tests目录
```bash
python3 -m unittest discover -s tests
```

## 命令回显与日志

- 长任务在交互终端使用固定刷新区域：首行始终为 `[模块 m/n] 当前动作`，下方只显示
  实时尾部；尾部按终端高度取约三分之一，限制为 10 至 24 行。禁止把完整子进程输出
  持续追加到终端并淹没当前步骤。截断行时必须按终端显示单元计算中文等宽字符，
  不能直接按 Python 字符数截断，否则隐式换行会破坏 ANSI 原地刷新。
- 成功后收起刷新区域并输出一行结果；失败时保留末尾诊断，并明确指出完整日志位置。
  已输出的远端错误不得再包装成无信息的“SSH 失败”。
- Terminal 每次调用都将命令、时间与退出结果追加到项目目录的
  `terminal.log`；由 Terminal 执行的本地编排、SSH/SFTP、系统准备与部署过程也将完整
  输出追加到该文件。每次调用先写入 `tmp/terminal-logs/` 中的独立片段，结束后持锁整块
  合并，禁止多个进程直接交叉追加；日志块必须包含 operation ID、命令、实际回显、退出
  结果和耗时，成功记录不能只有 `exit=0`。
- Manager 业务命令在远端部署目录写入 `manager.log`；聚合调度另写 `schedule.log`，
  Minecraft 自身输出仍由整合包/Docker 日志管理。
- Monitor 业务命令在远端部署目录写入 `monitor.log`，容器服务输出由 Docker 日志管理。
  Terminal 只展示实时尾部，不替代远端业务日志。
# 目录结构
/lib/{cli.py,config.py,deploy.py,docker.py,docker-install.sh,manager.py,monitor.py,power.py,remote.py,providers/}
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
src/{app.py,config.py,network.py,power.py,schedule.py,qqbot.py,message.yml,providers/,requirements.txt}
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
- SSH：部署使用 `steam@mc.tunarund.top`，应急迁移与回滚可用
  `root@mc.tunarund.top`；均已配置免密登录。
- 2026-10-18 已将 `/root/closing-song` 迁移为 Manager 整合包 `closing-song`：
  Docker 容器使用 Java 17、端口 27015、13G 内存上限和 Runtime 默认 JVM 参数。
- 迁移、回档与进服验证完成后，旧 `/root/closing-song` 以及旧 `minecraft`、`mc-watchdog`、
  `mc-nightly-shutdown` 的 systemd unit/备份文件已删除；新 Manager 备份是唯一回档来源。
- 首份 Manager 备份已经过 gzip 完整性校验；迁移后根分区剩余约 31 GB。
- 为缩短最终停服同步，主机额外安装了 Debian `rsync`；MCSP 本身不依赖它。
- Manager 每天 23:00 优雅停止全部整合包；Monitor 每天 20:00 开机、23:05 关机。
  Lighthouse 旧 MC 开关机/guard cron 块已移除，ACME cron 与旧 QQ Bot 暂时保留。

# 测试原则
- 先在 Ubuntu 虚拟机验证 Terminal 源码安装、配置检查、帮助信息和错误提示。
- 远端先执行只读盘点和 `deploy manager|monitor --test`；没有明确迁移方案前，不在生产 Manager 上执行部署、安装 Docker、停服或电源操作。
- Monitor 首次部署前先停用或隔离旧 QQ Bot，避免同一机器人重复消费事件。
- 发行版测试留到 `release.py` 实现之后。

# TODO
- Manager 迁移实测问题（2026-10-18）
  - 整合包 ID 在 Terminal 与 Manager 两层都要检查；这是跨 SSH 信任边界的重复校验，
    统一规则为大小写字母、数字和减号，`template` 保留。
  - Manager/Docker 以执行 `manager.sh` 的用户 UID/GID 运行。嵌套文件 bind mount 的
    目标若不存在，Docker 会在外层 `src/` 创建 root 占位文件；Manager 必须预创建目标。
  - `mcsp manager start` 在长时间 Forge 初始化期间的终端回显不足；Manager 已每两秒产生
    健康进度，仍需检查 Terminal 固定尾部在真实交互终端中的刷新和 SSH 分块行为。
  - 启动超时必须覆盖大型整合包冷启动；本次 264 模组正常约 85 秒，网络无限等待则在
    300 秒后失败。Runtime 默认加入连接/读取超时和 Forge 自动确认，GC 参数保持精简。
  - 失败容器的删除应进入 Manager 公共接口，Terminal 不应直接实现 Docker 操作；需区分
    删除容器/网络与删除整合包数据，默认绝不能删除 bind mount 数据。
  - 备份和恢复必须统一通过 Manager；新增备份列表、短 ID 和安全的自动停服恢复重启。
  - `read-only.enabled=false` 时整个 `src/` 可写，`persistent` 仍决定哪些路径分离到 `data/`
    并进入备份；`enabled=true` 时 `src/` 只读，只有 persistent 挂载路径可写。
  - 部署迁移目录且已开启自动备份时，聚合调度可能在最终切换前立即生成首份备份；部署
    流程需要“未激活/维护中”状态，避免把预复制中的数据标为可恢复备份。
  - Monitor 在首次部署时会把实例收敛到当前游玩窗口应有的电源状态；窗口外维护可能立即
    触发关机。需要维护模式或“首次启动不补执行旧事件”，部署文档也必须先停止旧调度。
  - Docker daemon 重启会使部署 SSH 会话提前断开，而远端后续命令可能仍在运行；Terminal
    需要明确识别连接中断，禁止把未知结果当成功并发起第二次部署。
- monitor qqbot
  - qqbot实现取消预订，告警时可忽略 1/2/5h。
- Docker 环境准备只属于 Terminal `lib/docker.py`；Manager/Monitor 仅检查并使用 Docker，
  不内含系统安装逻辑。Shell bootstrap 作为 `lib/docker-install.sh` 资源在源码版
  动态读取；release 必须把它打入单文件产物，不在发行目录外显。
- 更新*.md /test/
- 实现release.py
release.py 构建发行版（未实现）
将`/**/lib` python运行环境打包，生成`*.sh`的二进制
然后将manager monitor env config readme打包压缩，发布Linux和Windows版
二期
- 扁平化config.yml
去掉.monitor.manager的power-api
只填.monitor.manager
去掉.manager.power-api
- 统一命令回显与日志记录规范
- 实现cos备份整合包或存档
