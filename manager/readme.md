# 账号准备
useradd username
usermod -aG sudo,docker username
echo 'username ALL=(ALL:ALL) NOPASS | sudo tee /etc/sudoers.d/username'

# ssh免密登录
ssh-keygen

~username/.ssh/authorized_keys 写入公钥，如id_ed25519.pub的内容

Done，如出错或不知如何进行，自行咨询D老师

# 整合包标准目录结构
参考./modpacks/template
```
template/
├── backups/
├── compose.yml  # 执行 config/start 后生成
├── config.yml
├── data/
└── src/
    ├── banned-ips.json
    ├── banned-players.json
    ├── config/
    ├── crash-reports/
    ├── eula.txt
    ├── logs/
    ├── ops.json
    ├── server.properties
    ├── usercache.json
    ├── user_jvm_args.txt
    ├── usernamecache.json
    ├── whitelist.json
    └── world/
```
|目录|作用|
----|----
src/        | 存放整合包实际包含的文件
config.yml  | 配置文件
data/       | 存放运行时动态修改的文件和目录，由persistent指定
backups/    | 存放备份文件，一般是persistent指定路径的压缩包
compose.yml | 由config.yml自动生成，指导docker容器创建

# Manager 命令

`compose.yml` 是生成物，不要手工修改。将服务端文件放入 `src/`，并根据实际
启动脚本修改 `config.yml` 中的 `command`。

```bash
cp config.yml.example config.yml
./manager.sh create example
./manager.sh config example
./manager.sh start example
./manager.sh status example
./manager.sh logs example --lines 100
./manager.sh logs example --follow
./manager.sh console example say hello
./manager.sh backup example
./manager.sh stop example
./manager.sh restore example
./manager.sh remove example --force
./manager.sh stop-all
./manager.sh schedule install
./manager.sh schedule run
./manager.sh schedule remove
```

`start` 会按 Java 版本与运行时文件内容哈希构建镜像，并在容器健康检查通过后返回。
修改 Dockerfile 或 `entrypoint.sh` 会自动得到新镜像标签，不会静默复用旧运行时。

## 自动备份

整合包 `config.yml` 的 `backup.enabled` 为 `true` 时，`interval` 决定备份间隔，
支持分钟、小时和天，例如 `30m`、`6h`、`1d`。`retain` 控制最大保留份数。

```yaml
backup:
  enabled: true
  interval: 6h
  retain: 7
  quiesce-seconds: 5
```

`schedule install` 只修改当前 Manager 用户的 crontab，每分钟检查一次到期备份。
实际备份仍按 `interval` 执行，全局锁会防止上一次备份未完成时重复启动。
开启自动备份但尚无任何备份的整合包，会在下一次检查时立即建立首份备份。
