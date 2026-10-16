# 概览
MC服务端管理器
提供常见的java8、17、21环境，支持多实例启动、定时开关机qq机器人控制、便捷备份恢复

简易架构图
```mermaid
flowchart LR
  T[Terminal] -->|ssh:deploy| M[Manager]
  T -->|ssh:deploy| G[monitor]
  G -->|api:power| M

  classDef dashed stroke-dasharray:5 5,stroke:#e91e63,stroke-width:2px;
  class G dashed;
```
api指云厂商提供的服务器实例控制api接口
虚线表示可选开启

manager和monitor可以独立部署，terminal仅作为控制编排
基本都需要python和docker环境
## terminal
个人PC、中控、部署manager与monitor
支持Windows与Linux
### 功能
- 存放配置与密钥
- 部署、管理Manager与monitor
- 整合包与存档临时备份点


## manager
实际管理运行整合包的服务器，大内存、一般按量计费、定时开关
### 功能
- 整合包生命周期管理
创建、启动、状态查询、日志查询、执行控制台指令、停止、备份、恢复、移除
- 定时关机与服内广播
用户级cronjob定时触发指令
- 容器化
按需构建java镜像作为整合包运行环境，挂载整合包目录启动容器
通过docker方便实现开机自启

## monitor
可选的7x24h值守，方便定时自动开关机与qq机器人交互管理
需包年包月的服务器，配置1C2G即可
### 功能
- QQ机器人（后续接入）
通过QQ消息查询云主机电源状态、开机和关机
- 7x24h值守
可在指定时间自动让Manager开关机
内部定时器周期性检查并提交幂等开关机，不依赖 cron

Monitor 只调用云电源 API，不通过 SSH、TAT 或 `manager.sh` 执行 Manager 内部指令。
## 当前本地使用

```bash
python3 -m pip install -r lib/requirements.txt
./mcsp.sh config check config.yml.example
cp config.yml.example config.yml
cp .env.example .env

# 检查并部署 Manager
./mcsp.sh deploy manager --check
./mcsp.sh deploy manager

# 在本地准备整合包，填充 src/ 后上传
./mcsp.sh pack create example
./mcsp.sh pack deploy example
./mcsp.sh pack start example

# Manager 云主机电源
./mcsp.sh manager status
./mcsp.sh manager start
./mcsp.sh manager stop

# 部署 Monitor 后验证 QQ 私聊，并更新名为 owner 的目标
./mcsp.sh monitor add-target
# 也可以指定其他目标名称
./mcsp.sh monitor add-target backup-owner
```

`manager stop` 会先通过 SSH 执行远端 `manager.sh stop-all`，成功后才调用电源 API；
只有显式使用 `--force` 才允许停服失败后继续关机。详细 Manager 命令见
[manager/readme.md](./manager/readme.md#manager-命令)。
## 1.账号
需持有Linux服务器与sudo账号
[账号创建](./manager/readme.md#账号创建)
可以使用密码，但更建议配置[ssh免密登录](./manager/readme.md#ssh免密登录)
## 2.整合包
zip、tar.gz都行，支持从url下载
[整合包标准目录结构](./manager/readme.md#整合包标准目录结构)

整合包 ID 仅允许大小写英文字母、数字和减号（`A-Z`、`a-z`、`0-9`、`-`）。
# 详情
## 目录结构
/lib/{cli.py,config.py,deploy.py,power.py,remote.py,manager/,monitor/,providers/}
/lib/manager/{service.py,deploy.py,modpacks.py}
/lib/monitor/{service.py,deploy.py,config.py}
/lib/providers/{tencent_api.py,example_api.py}

/mc.{sh,exe}
/{config.yml,.env,README.md}
配置、密钥、说明

/manager/{readme.md,manager.sh,lib/,runtime/,modpacks/}
lib/{archive.py,cli.py,config.py,docker.py,modpacks.py,power.py,schedule.py,providers/}
lib/providers/{tencent_api.py,example_api.py}
runtime/{java{8,17,21}.dockerfile,entrypoint.sh}
modpacks/template/{src/,data/,backups/,config.yml}

/monitor/{src/,data/,Dockerfile,compose.yml,monitor.sh}
src/{app.py,config.py,monitor/,qqbot/,requirements.txt}
src/monitor/{service.py,power.py,schedule.py,providers/}
后续可能改go实现

/dev存放开发时需要的工具
release.py 构建发行版
spec.md 当前文档

# 定时开关机
## monitor

Monitor 保留 `monitor.manager.auto-start/start-at/auto-stop/stop-at` 配置。
每 30 秒检查一次云主机状态；每天的开、关机事件成功处理后写入
`monitor/data/state.json`，失败会在下一周期重试。进程重启会补处理当前时间线上
最近的事件。

生产镜像会复制 `monitor/src`，运行时只挂载只读 `config.yml` 和可写 `data/`；
不挂载源码，以保证同一镜像的行为可复现。

## 电源 API

Manager 和 Monitor 各自通过 `power-api` 配置目标实例。`provider` 是适配器 ID，
仅允许大小写字母、数字和减号。例如 `provider: xxxaaa` 会自动寻找
`providers/xxxaaa_api.py`；`tencent` 是内置的腾讯云适配器：

```yaml
power-api:
  provider: xxxaaa
  instance-id: minecraft-manager
  instance-region: local
```

自定义 provider 必须实现 `status`、`start`、`stop` 三个函数，每个函数的签名为
`(instance_id, region, secret_id, secret_key) -> dict`。加载时会检查文件、
必要函数和参数签名；`Power` 负责传入实例信息与 AK/SK，provider 不依赖
`power.py`。可复制 `example_api.py` 为 `xxxaaa_api.py`，URL 与厂商签名逻辑都写在该文件内。

`example_api.py` 展示的转接口使用 `POST`，从 `.env` 读取的
`POWER_SECRET_KEY` 会作为 Bearer Token；
只有一个 Token 时将它填入 `POWER_SECRET_KEY`，`POWER_SECRET_ID` 留空。
请求 JSON 包含 `action`（`status`、`start` 或 `stop`）、`instance-id` 和
`instance-region`。`status` 必须返回规范化状态：

等价的 `curl` 调用如下，启动或关机只需将 `status` 改为 `start` 或 `stop`：

```bash
curl --request POST 'https://example.com/mcsp/power' \
  --header "Authorization: Bearer ${POWER_SECRET_KEY}" \
  --header 'Accept: application/json' \
  --header 'Content-Type: application/json' \
  --data '{
    "action": "status",
    "instance-id": "minecraft-manager",
    "instance-region": "local"
  }'
```

```json
{"state": "RUNNING", "public-ip": "203.0.113.1"}
```

`public-ip` 可省略；`start`、`stop` 可返回空对象。状态使用
`RUNNING`、`STARTING`、`STOPPING`、`STOPPED`。

## 腾讯云编程子用户
https://console.cloud.tencent.com/cam
新建用户-快速创建
访问方式： 控制台登录× 编程访问√
用户权限-新建自定义策略-按策略语法创建-空白模板-下一步
复制粘贴
```json
{
  "statement": [
    {
      "action": [
        "cvm:DescribeInstances",
        "cvm:StartInstances",
        "cvm:StopInstances"
      ],
      "effect": "allow",
      "resource": [
        "*"
      ]
    }
  ],
  "version": "2.0"
}
```
创建完成，回到https://console.cloud.tencent.com/cam
点击用户名-API密钥-新建密钥
成功获取POWER_SECRET_ID POWER_SECRET_KEY
# TODO
- 重构中。 参考 dev/refactor-plan
- 实现cos
