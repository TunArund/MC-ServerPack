# MC-ServerPackH 重构边界

## 结论

`MC-ServerPackH` 是一个新实现，不是对旧工程的目录改名。旧工程在新工程完成端到端验收前保持可运行，只作为行为参考和应急回退版本。

三个运行时边界固定为：

- Terminal：保存配置和密钥，通过 SSH 部署并编排。
- Manager：管理本机整合包、Docker 容器、备份、优雅停服和自身的定时关机。
- Monitor：独立管理远端云主机电源、定时值守、告警和 QQ 入口。

不允许运行时之间直接 import；交互只通过 SSH/TAT、云 API 和稳定的命令行契约。

## 命令语义

命令设计遵循以下原则：

1. **不重复已知上下文**：`manager.sh` 的生命周期动作默认作用于整合包，不再重复一层 `pack`；
   只有调度器这种独立资源使用 `schedule` 子命令。
2. **动词在前、资源在后**：统一为 `<action> <id>`，便于帮助、补全和权限审计。
3. **不同层级不共用裸命令**：Terminal 的云主机电源放在 `mcsp manager`，
   整合包放在 `mcsp pack`，避免两种 `start/stop` 混淆。
4. **危险操作必须显式**：有数据的删除需要 `--force`，恢复必须停服，资源超限不自动顶掉其他服务。
5. **未实现不假成功**：命令只在完整实现后加入 CLI，失败使用非零退出码。
6. **聚合调度、策略下沉**：系统只安装一个 `schedule run` cron；各整合包自行配置
   备份开关、相对周期和保留数，`manager.modpack` 配置统一停服，`manager` 配置云关机。
7. **标识符先校验再传输**：整合包 ID 只允许 ASCII 大小写字母、数字和减号；Terminal
   在 SSH 前校验，Manager 在访问本机目录前再次校验。

稳定命令层级：

```text
mcsp deploy manager|monitor
mcsp manager start|stop|status
mcsp pack create|config|start|stop|status|logs|console|backup|restore|remove <id>

manager.sh create|config|start|stop|status|logs|console|backup|restore|remove <id>
manager.sh stop-all
manager.sh schedule run|install|remove
```

Monitor 手动关机时通过 TAT 调用 `manager.sh stop-all`，无论停服是否成功都继续请求云关机，并对风险告警。

Manager 的调度闭环已经具备：单 cron 聚合检查、每包独立周期备份、分阶段关服广播、
全量优雅停服、截止窗口内重试，以及可选的幂等腾讯云关机。调度状态按日期持久化，
跨午夜截止窗口仍归属前一天的停服事件。

## 唯一调度所有者

同一个云主机关机事件不能同时由 Manager cron 和 Monitor 主动执行。目标语义是：

1. Manager 负责到点广播、停止全部整合包，然后提交一次云关机。
2. Monitor 负责定时开机和关机窗口内的状态检查；不重复提交普通关机。
3. 超过截止时间仍未关机时，Monitor 只告警。强制关机仅用于用户明确发起的手动关机流程。

Terminal 侧 `config.yml` 中的 `manager`、`monitor` 是两份可独立部署的完整配置段。
部署时只提取目标段并保持层级和键顺序，不在部署代码中重命名、搬移或重新组合字段。
两段中的地域、实例 ID 等重复项是有意设计：部署后两个运行时各自持有配置，不依赖
Terminal 在线，也不读取对方的配置文件。

## 迁移顺序与验收

1. 基础契约：配置校验、命令行层级、三运行时边界测试。
2. Manager 最小闭环：单个整合包的配置渲染、启停、状态、日志和控制台命令。
3. Manager 数据安全：持久化路径、备份/恢复、内存与并发限制、定时停服。
4. Terminal 部署：已实现环境检查、保留运行数据的源码同步、独立配置/最小密钥分发、
   整合包上传、远程 Manager 命令，以及先停服后软关机的云电源控制。
   Terminal 内部由 `cli.py` 解析和编排，`deploy.py` 负责部署，`modpacks.py` 负责整合包及
   TAT 停服，`power.py` 只负责实例电源，`remote.py` 负责 SSH/SCP，`tencent.py` 隔离 SDK。
5. Monitor：先做纯时间线和云电源状态机，再接 QQ；云 API 必须可注入假实现离线测试。
6. 发布与切换：干净环境安装测试、真机开停演练、备份恢复演练，然后才将 H 设为主工程。

每个阶段都必须同时具备成功路径、失败路径和幂等性测试。不以“文件已复制”作为迁移完成标准。
