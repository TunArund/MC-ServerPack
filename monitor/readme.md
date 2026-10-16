# Monitor

Monitor 是可独立部署的云主机电源控制器。它只调用 `power-api` 查询、开启或关闭
Manager 云主机，不使用 SSH、TAT，也不执行 `manager.sh`。

## 部署与命令

```bash
cp config.yml.example config.yml
cp .env.example .env
./monitor.sh up
./monitor.sh status
./monitor.sh logs --follow
./monitor.sh down
```

`auto-start/start-at` 和 `auto-stop/stop-at` 分别控制每日定时开、关机。内部调度器
每 30 秒检查一次；完成的事件记录在 `data/state.json`，API 临时失败会自动重试。
重复关机以及 Manager 与 Monitor 同时关机按云 API 的幂等语义处理。

## 容器边界

Docker 构建时将 `src/` 和依赖复制进镜像。运行时仅挂载：

- `config.yml`：只读配置；
- `data/`：调度状态与健康检查文件。

`.env` 只作为环境变量注入，不复制进镜像；`.dockerignore` 也会将密钥、配置和
运行状态排除在构建上下文之外。
