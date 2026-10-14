# 示例整合包

这个目录只提供容器配置和低内存 JVM 参数，不包含 Minecraft、Forge 或第三方 Mod
二进制。

## 准备 Forge

1. 从 [Minecraft Forge 官方下载页](https://files.minecraftforge.net/net/minecraftforge/forge/)
   下载所需版本的 Installer。
2. 在一个空目录运行 `java -jar <forge-installer.jar> --installServer`。
3. 将生成的完整服务端（包括 `libraries/` 和 `run.sh`）放入 `src/`。
4. 确认 `run.sh` 引用的 Forge 版本与 `libraries/` 中实际版本一致。

最终结构：

```text
template/
├── compose.yml
├── config.yml
├── src/
│   ├── run.sh
│   ├── user_jvm_args.txt
│   ├── libraries/
│   ├── mods/
│   └── config/
├── data/
│   ├── world/
│   ├── logs/
│   ├── crash-reports/
│   ├── eula.txt
│   └── server.properties
└── backups/
```

## 首次启动与 EULA

```bash
./manager.sh start template
./manager.sh logs template
```

模板会在 `data/eula.txt` 建立 `eula=false`。请先阅读
[Minecraft EULA](https://www.minecraft.net/en-us/eula)，只有接受后才手工改为
`eula=true`。需要跨部署保留的路径由 `config.yml` 的 `read-only.persistent` 控制；
启用 `read-only.enabled` 后，这些路径也同时是只读服务端目录中的可写例外。新产生
的持久化缓存应加入该列表。项目不会替用户预先接受 EULA。

## 添加 Mod

先确认每个 Mod 的许可证或作者分发条款允许服务端使用和再分发。私用时可在
`src/mods/` 放置 Mod；公开发布时更稳妥的做法是
只分发 Mod 清单、版本、哈希和官方下载地址，由使用者自行下载。
