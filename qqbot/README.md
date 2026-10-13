# QQ Bot 与 CVM 控制器

这是部署到 `lighthouse` 的独立 Compose 单元。

- `src/qqbot-menu.py`：QQ 菜单与 TAT 操作
- `src/cvm-power.sh`：腾讯云 CVM 开关机、状态和 guard
- `entrypoint.sh`：生成运行配置、启动定时器与 QQ Bot
- `data/`：日志、冷却、取消预订和调度状态，不随部署覆盖

计划关机时，manager 先按 `MC_PLAY_END` 优雅停服；本控制器等到
`MC_POWEROFF_AT` 后调用 `StopInstances` 普通关机并复查状态。普通关机失败时，
只有确认 Minecraft 端口已经关闭才使用 `StopType=HARD`，否则保留实例并告警。

不要直接在本目录维护 `.env`；根目录的 `mc.sh deploy gateway` 会从仓库根 `.env`
生成最小配置并上传。
