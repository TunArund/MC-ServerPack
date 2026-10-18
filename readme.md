# 概览
MC服务端管理器
提供常见的java8、17、21环境，支持多实例启动、定时开关机qq机器人控制、便捷备份恢复

简易架构图
```mermaid
flowchart LR
  T[Terminal] -->|Python SSH/SFTP:deploy| M[Manager]
  T -->|Python SSH/SFTP:deploy| G[monitor]
  G -->|api:power| M

  classDef dashed stroke-dasharray:5 5,stroke:#e91e63,stroke-width:2px;
  class G dashed;
```
api指云厂商提供的服务器实例控制api接口
虚线表示可选开启

Manager 和 Monitor 可以独立部署，Terminal 只负责控制编排。
Terminal 与 Manager 需要 Python 3.9+；Manager 和 Monitor 部署目标需要可用的 Docker与Compose
## 组件
### terminal
个人PC、中控、部署manager与monitor
支持Windows与Linux

- 存放配置与密钥
- 部署、管理Manager与monitor
- 整合包与存档临时备份点

### manager
实际管理运行整合包的服务器，大内存、一般按量计费、定时开关

- 整合包生命周期管理
创建、启动、状态查询、日志查询、执行控制台指令、停止、备份、恢复、移除
- 定时关机与服内广播
用户级cronjob定时触发指令
- 容器化
按需构建java镜像作为整合包运行环境，挂载整合包目录启动容器
通过docker方便实现开机自启

### monitor
可选的7x24h值守，方便定时自动开关机与qq机器人交互管理
需包年包月的服务器，配置1C2G即可

- QQ机器人
通过QQ消息查询云主机电源状态、开机和关机
- 7x24h值守
可在指定时间自动让Manager开关机
内部定时器周期性检查并提交幂等开关机，不依赖 cron

## 快速上手
Linux ./mcsp xxx
Windows ./mcsp.exe xxx
Python源码用户
```bash
python3 -m pip install -r lib/requirements.txt
./mcsp.sh xxx
```
Terminal 使用 Python SSH/SFTP 客户端，不要求本机安装或启用 OpenSSH，也不要求
远端安装 rsync；远端只需提供 Python 3。首次连接未知主机时会显示主机密钥指纹，
输入 `yes` 后记录到 `~/.ssh/known_hosts`。
最小部署仅需manager，要求docker compose环境

```bash
# 1.配置
./mcsp.sh config init
# 填写config.yml 后执行
./mcsp.sh config check

# 2.部署 Manager
./mcsp.sh deploy manager               # 完整源码部署（包含 Docker 与 Python）

# 3.上传启动 整合包
./mcsp.sh local manager create example # 创建本地整合包骨架
# 将整合包解压到 src，阅读 EULA 后在 config.yml 中设置 eula
./mcsp.sh deploy modpack example # 上传到manager
./mcsp.sh manager list                # 列出远端整合包
./mcsp.sh manager start example       # 根据整合包 config.yml 远程启动
# 读取或修改实际生效的 server.properties
./mcsp.sh manager property example online-mode
./mcsp.sh manager property example online-mode false
```

# 参考
## Terminal命令
```bash
./mcsp.sh deploy manager --create-user # 可选：创建部署用户
./mcsp.sh deploy manager --install-docker # 只安装或验证 Docker
./mcsp.sh deploy manager --install-python # 源码版：安装 Python、venv 与依赖
./mcsp.sh deploy manager --source      # 只同步 Manager 程序文件
./mcsp.sh deploy manager --config      # 同步配置、密钥并应用
./mcsp.sh deploy monitor --source      # 只同步 Monitor 程序文件
./mcsp.sh deploy monitor --config      # 同步配置、密钥并应用

# Manager 电源控制
./mcsp.sh status manager
./mcsp.sh start manager
./mcsp.sh stop manager

# Monitor 容器电源控制（宿主机保持 7x24h 运行）
./mcsp.sh status monitor
./mcsp.sh start monitor
./mcsp.sh stop monitor

# 部署 Monitor 后验证 QQ 私聊，并更新名为 owner 的目标
./mcsp.sh monitor add-target
# 也可以指定其他目标名称
./mcsp.sh monitor add-target backup-owner
```
## Manager
[manager/readme.md](./manager/readme.md#manager-命令)。
## Monitor配置
需要腾讯云API密钥控制manager定时开关机（支持其他厂商API，但需自行参考dev/agents.md#电源API 实现）
填入 .env POWER_SECRET

## 整合包
zip、tar.gz都行，支持从url下载
[整合包标准目录结构](./manager/readme.md#整合包标准目录结构)

整合包 ID 仅允许大小写英文字母、数字和减号（`A-Z`、`a-z`、`0-9`、`-`）。

`local manager create <id>` 只创建整合包骨架，自行覆盖 `src/` 并调整
`config.yml`。
原版服务端从 [Minecraft 官方下载页](https://www.minecraft.net/download/server)
或所需历史版本的官方发布页下载。在整合包 `config.yml` 中设置 `eula: true`
表示接受 [Minecraft EULA](https://www.minecraft.net/eula)。默认只需将一个服务端 JAR
放入 `src/`：普通 JAR 自动启动，Forge 安装器自动以 `--installServer` 准备后启动。
用户无需维护 `command` 或 `jvm-args`；多个候选入口等特殊情况才使用高级覆盖配置。
Manager 不隐式读取 `run.sh`、`run.bat` 或 `user_jvm_args.txt`。
同名目录默认拒绝创建；`create --force` 会完整替换其 `src`、`data`、
`backups` 和配置。

`local manager <args...>` 会将后续参数原样传给本机 `manager.sh`，因此
`create`、`remove`、`config`、`start` 等 Manager 命令均可直接使用。

`deploy modpack <id>` 会先调用本地 Manager 校验配置和启动文件，再将 `src/` 与
`config.yml` 增量同步并更新到远端；重复执行就是更新，不需要 `--force`。远端已有的
`data/` 和 `backups/` 始终保留；新配置无法应用时会恢复更新前的 `src/` 与配置。

## 账号
需持有 Linux 服务器与非 root 的免密 sudo 账号；配置中的 `manager.user` 和
`monitor.user` 均不允许填写 `root`。若新服务器目前只有 root，可执行：

```bash
./mcsp.sh deploy manager --create-user
./mcsp.sh deploy monitor --create-user
# 默认依次寻找 ~/.ssh/id_ed25519.pub、id_ecdsa.pub、id_rsa.pub
./mcsp.sh deploy manager --create-user --key ~/.ssh/id_ed25519.pub
```

`./mcsp.sh deploy` 部署 Manager 和已启用的 Monitor；`deploy --test-all` 只检查这些
运行时。单目标检查使用 `deploy manager --test` 或 `deploy monitor --test`。

命令只从对应的 `manager` 或 `monitor` 配置读取 host，不读取其中的 user；它先
询问现有管理员 SSH 用户名（直接回车默认为 `root`），回显并使用私钥测试登录；
登录成功后才询问要创建的部署用户名，并列出公钥写入等即将执行的步骤。
这里只允许 `root` 作为引导账号；配置中的实际部署用户仍禁止为 `root`。非 root
管理员需要已经具备免密 sudo。创建后需将配置中的 user 改为刚创建的用户名。
命令会安装公钥并授予新用户免密 sudo；若服务器已有 `docker` 组，也会将新用户
加入该组。创建后命令即退出，不会同时部署运行时。
[Docker 安装](./manager/readme.md#docker-安装)由 Terminal 直接通过 SSH 执行，不依赖
远端已存在 Manager 或 Monitor 源码；可分别使用 `deploy manager --install-docker`
和 `deploy monitor --install-docker`。完整部署也会幂等安装或验证 Docker。
[账号创建](./manager/readme.md#账号创建)

## 腾讯云API密钥获取
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
## 腾讯云按量计费实例固定IP
为了省钱，关机选项默认为：关机不计费
> 参考`lib/providers/tencent.py`
所以需要将cvm的IP调整为 弹性公网IP
否则每次开关机IP都会变动
