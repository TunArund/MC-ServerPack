# Terminal

Terminal 是 MC-ServerPack 的本地控制端，负责读取总配置、准备远端账号和 Docker、
部署 Manager/Monitor，并通过 Manager API 执行日常管理命令。

## 目录

```text
terminal/
├── mcsp.sh
├── config.yml.example
├── .env.example
├── requirements.txt
├── src/
│   ├── config_sdk.py
│   ├── docker_sdk.py
│   ├── manager_sdk.py
│   ├── modpack_sdk.py
│   ├── observability_sdk.py
│   ├── power_sdk.py
│   └── providers/
├── logs/
├── modpacks/
│   └── template/
└── ...
```

Terminal 不在运行时导入仓库根目录的 `lib/`。共享 SDK、Provider 与 Observability 以
`lib/` 为源码真源，并在 `terminal/src/` 中携带受测试约束的副本，因此发布包可以独立运行。

首次使用：

```bash
python3 -m pip install -r terminal/requirements.txt
./terminal/mcsp.sh config init
# 编辑 terminal/config.yml 和 terminal/.env
./terminal/mcsp.sh config check
```

所有命令可在模块名前使用全局输出选项：

```bash
./terminal/mcsp.sh --quiet deploy manager
./terminal/mcsp.sh --json manager list
```

默认最终结果写 stdout，进度和错误写 stderr。`--quiet` 隐藏进度与成功提示；`--json`
保证 stdout 只输出一个 JSON 文档，两者互斥。

Manager 和 Monitor 的部署及跨组件命令见项目根目录
[readme.md](../readme.md)。

本地整合包固定保存在 `terminal/modpacks/`：

```bash
./terminal/mcsp.sh modpack create example
./terminal/mcsp.sh modpack check example
./terminal/mcsp.sh modpack export example
./terminal/mcsp.sh deploy modpack example
```

`deploy modpack` 只上传 `terminal/modpacks/<id>.tgz`，不会隐式重新打包。
