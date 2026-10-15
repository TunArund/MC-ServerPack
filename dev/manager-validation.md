# Manager 真实整合包验证

验证日期：2026-10-15（Asia/Shanghai）

使用本地真实整合包“登神长阶 0.1”的 427MB Forge 1.20.1 服务端文件，
复制到受 `.gitignore` 保护的 `ascension-e2e` 临时整合包。源整合包和旧仓库未被修改。

验证结果：

- 配置校验与 Compose 渲染成功，首次生成 13MB 持久化数据。
- 离线备份成功；修改 `data/eula.txt` 后恢复，文件内容确实回到备份版本。
- 运行时文件哈希生成镜像 `mc-java:17-0ea1f011d991`，未复用旧工程的固定标签镜像。
- Forge 服务器在 27.811 秒内完成加载，Compose 状态达到 `healthy`，25565 端口就绪。
- `console` 发送 `say MC-ServerPackH-E2E`，服务器日志收到该消息。
- 运行中备份先执行 `save-off` 和 `save-all flush`，日志确认所有维度已保存，完成后 `save-on`。
- `stop` 通过命名管道发送 Minecraft `stop`，容器最终为 `exited 0`。

验证后已通过 `manager.sh remove ascension-e2e --force` 清理临时容器、网络、427MB 数据副本和备份。
