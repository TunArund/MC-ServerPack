# 开发说明

## 边界

- `lib/`：个人 PC 使用的跨平台控制核心。
- `manager/`：同步到 mc-server 的运行与管理代码。
- `qqbot/`：同步到 gateway 的 QQ Bot、电源控制和定时任务。
- `dev/`：不进入发行包的开发与发布工具。

根 `config.yml` 是部署拓扑，manager 部署时只提取 `manager.modpack`。整合包运行规格
位于各自的 `manager/modpacks/<ID>/config.yml`，Compose 由它生成。

容器 UID/GID 默认取执行 `manager.sh` 的用户。`src/` 只读，声明为
`dynamic-paths` 的内容初始化到 `data/`，已有持久化数据不得被部署覆盖。备份与恢复
只操作 `world/`。

提交公开仓库和制作发行包前，检查其中没有 `.env`、私有整合包、Minecraft/Forge
文件、Mod、世界、玩家数据、日志、备份或 `eula=true`。

## 检查

```bash
python -m compileall -q lib manager/lib qqbot/src dev
bash -n mc.sh manager/manager.sh qqbot/entrypoint.sh
git diff --check
```

## 发布

Windows 和 Linux 必须分别在对应系统构建。安装开发依赖：

```bash
python -m pip install -r dev/requirements-dev.txt
```

发布脚本从进程环境读取 `GITEE_TOKEN`，未设置时读取根 `.env`。版本使用 `v0.1`
格式：

```bash
git tag -a v0.1 -m "v0.1"
git push origin main v0.1
python dev/release.py v0.1
python dev/release.py v0.1 --publish
```

先在 Linux 执行一次，再在 Windows 的同一标签上执行一次。脚本会复用 Gitee
Release，并上传 `dev/dist/` 中当前平台的 ZIP 和 SHA-256 文件。

平台包不包含根 `lib/` 和 `.gitignore`；Windows 包也不包含 `mc.sh`。
`manager/lib/` 是 mc-server 运行时代码，必须保留。
