# Monitor

Monitor 是可独立运行的云主机电源控制器。它只调用 `power-api` 查询、开启或关闭
Manager 云主机，不使用 SSH、TAT，也不执行 `manager.sh`。

## 部署与命令

```bash
cp config.yml.example config.yml
cp .env.example .env
./monitor.sh check
./monitor.sh install
./monitor.sh status
./monitor.sh logs --follow
./monitor.sh stop
./monitor.sh start
```

`check` 只验证 Docker Engine 与 Compose 插件是否可用，不会安装或修改系统软件。
Monitor 不负责准备 Docker 系统环境；Terminal 部署时使用
`./mcsp.sh deploy monitor --install-docker`，手工独立部署则需先自行安装并授权 Docker。
`install` 在检查通过后构建并启动 Monitor 容器；日常启停使用 `start`、`stop`，它们
只操作 Monitor 容器，不关闭这台 7x24h 主机。

QQ 单聊目标应从顶层终端验证并写回唯一配置源：

```bash
./mcsp.sh monitor add-target          # 更新 owner
./mcsp.sh monitor add-target another  # 更新或新增 another
```

该命令生成随机四位验证码，远端 Monitor 只接受内容完全一致的 C2C 消息；验证后
将 `user_openid` 写回 `monitor.qqbot.targets`，再刷新远端配置。

`auto-start/start-at` 和 `auto-stop/stop-at` 分别控制每日定时开、关机。内部调度器
每 30 秒检查一次；完成的事件记录在 `data/state.json`，API 临时失败会自动重试。
重复关机以及 Manager 与 Monitor 同时关机按云 API 的幂等语义处理。

## 容器边界

Docker 构建时将 `src/` 和 `src/requirements.txt` 复制进镜像。运行时仅挂载：

- `config.yml`：只读配置；
- `data/`：调度状态与健康检查文件。

`.env` 只作为环境变量注入，不复制进镜像；`.dockerignore` 也会将密钥、配置和
运行状态排除在构建上下文之外。

## 源码边界

`src/` 是唯一 Python 源码根目录：`app.py` 负责组装，`config.py` 负责加载配置，
`power.py` 负责电源及 Provider 加载，`schedule.py` 负责日程、状态与服务循环，`network.py` 负责 QQ
网络收发，`qqbot.py` 负责权限、菜单和消息路由，`message.yml` 保存可编辑模板。
QQ Bot 不读取调度状态，两者只在 `app.py` 中获得同一个电源控制对象。
