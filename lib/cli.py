"""MCSP Terminal：解析命令并编排 Manager、Monitor 与电源接口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    CONFIG_FILE, ROOT, ConfigError, deployment_connection,
    deployment_endpoint, deployment_host, initialize, load, load_monitor,
)
from .deploy import (
    AccountError, AccountProvisioner, DeployError, DockerProvisioner,
    PythonProvisioner,
)
from .manager import Manager, ManagerError
from .monitor import Monitor
from .power import InstanceStatus, PowerError
from .provider import ProviderError, load_provider
from .remote import CommandError, Transport


SOURCE_MODE = not getattr(sys, "frozen", False)


class CliServices:
    """Terminal 只编排；实际操作全部委托给运行时或 Provider。"""

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, source_mode: bool = SOURCE_MODE,
    ) -> None:
        self.config_path = config_path
        self.source_mode = source_mode
        self.transport = Transport(ROOT / "deploy.log")
        self.accounts = AccountProvisioner(self.transport)
        self.manager: Optional[Manager] = None
        self._monitor_instance: Optional[Monitor] = None

    def manager_runtime(self) -> Manager:
        if self.manager is None:
            self.manager = Manager(self.config_path, transport=self.transport)
        return self.manager

    def monitor(self) -> Monitor:
        if self._monitor_instance is None:
            self._monitor_instance = Monitor(
                self.config_path, transport=self.transport,
            )
        return self._monitor_instance

    def check(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).check()

    def install(self, target: str) -> None:
        self.install_docker(target)
        if target == "manager" and self.source_mode:
            self.install_python()
        (self.manager_runtime() if target == "manager" else self.monitor()).install()

    def publish(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).publish()

    def configure(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).configure()

    def deploy_modpack(self, pack_id: str, *, force: bool = False) -> None:
        self.manager_runtime().deploy_modpack(pack_id, force=force)

    def install_docker(self, target: str) -> None:
        host, user = deployment_connection(self.config_path, target)
        DockerProvisioner(host, user, transport=self.transport).install()

    def install_python(self) -> None:
        if not self.source_mode:
            raise DeployError("--install-python 仅供源码版 Manager 使用")
        PythonProvisioner(
            deployment_endpoint(self.config_path, "manager"),
            ROOT / "manager/lib/requirements.txt", transport=self.transport,
        ).install()

    def local_create(
        self, pack_id: str, *, extra: Tuple[str, ...] = (),
    ) -> Path:
        return self.manager_runtime().create_local(pack_id, extra=extra)

    def manager_command(
        self, action: str, pack_id: Optional[str] = None, *,
        extra: Tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        self.manager_runtime().run(action, pack_id, extra=extra, tty=tty)

    def monitor_command(self, action: str) -> None:
        self.monitor().run(action)

    def add_monitor_target(self, name: str) -> str:
        return self.monitor().add_target(name)

    def account_context(
        self, target: str, key_path: Optional[Path] = None,
    ) -> tuple[str, Path]:
        return (
            deployment_host(self.config_path, target),
            self.accounts.prepare_key(key_path),
        )

    def test_account_login(
        self, host: str, administrator: str, key_path: Path,
    ) -> None:
        self.accounts.test_login(host, administrator, key_path)

    def create_user(
        self, host: str, username: str, administrator: str, key_path: Path,
    ) -> None:
        self.accounts.create(host, username, administrator, key_path)

    def deployment_targets(self) -> tuple[str, ...]:
        targets = ["manager"]
        if load_monitor(self.config_path).enabled:
            targets.append("monitor")
        return tuple(targets)

    def control(
        self, target: str, action: str, *, force: bool = False,
    ) -> Optional[InstanceStatus]:
        if target == "monitor":
            self.monitor_command(action)
            return None
        manager = self.manager_runtime()
        power = manager.power()
        if action == "status":
            return power.status()
        if action == "start":
            return power.start()
        current = power.status()
        if current.state == "RUNNING":
            try:
                manager.run("stop-all")
            except CommandError:
                if not force:
                    raise
        return power.stop(current)


def make_parser(*, source_mode: bool = SOURCE_MODE) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcsp", description="Minecraft 服务端部署与管理")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Terminal 配置文件")
    commands = parser.add_subparsers(dest="module", required=True)

    config = commands.add_parser("config", help="检查 Terminal 配置")
    config_commands = config.add_subparsers(dest="command", required=True)
    config_commands.add_parser("init", help="从示例初始化 config.yml 与 .env")
    check = config_commands.add_parser("check", help="验证 config.yml 与 Provider")
    check.add_argument("path", nargs="?", type=Path)

    deploy = commands.add_parser("deploy", help="部署运行时、配置或整合包")
    deploy.add_argument(
        "--test-all", action="store_true",
        help="只检查所有已启用运行时的本地材料、SSH 和远端依赖",
    )
    deploy_commands = deploy.add_subparsers(dest="deploy_kind")
    for target in ("manager", "monitor"):
        runtime = deploy_commands.add_parser(target, help=f"部署 {target.title()}")
        mode = runtime.add_mutually_exclusive_group()
        mode.add_argument(
            "--test", action="store_true",
            help="只检查该运行时的本地材料、SSH 和远端依赖",
        )
        mode.add_argument(
            "--create-user", action="store_true",
            help="交互选择管理员 SSH 账号，创建配置中的普通部署用户后退出",
        )
        mode.add_argument(
            "--source", action="store_true",
            help="只同步运行时程序文件，不检查或安装系统依赖",
        )
        mode.add_argument(
            "--config", dest="sync_config", action="store_true",
            help="只同步该运行时的 config.yml、.env 并应用",
        )
        mode.add_argument(
            "--install-docker", action="store_true",
            help="只安装或验证远端 Docker Engine 与 Compose 插件",
        )
        if target == "manager" and source_mode:
            mode.add_argument(
                "--install-python", action="store_true",
                help="只安装源码版 Manager 的 Python、pip、venv 与依赖",
            )
        runtime.add_argument(
            "--key", type=Path, help="创建用户所用 SSH 公钥；默认自动寻找",
        )
    deploy_pack = deploy_commands.add_parser("modpack", help="上传整合包")
    deploy_pack.add_argument("pack_id")
    deploy_pack.add_argument("--force", action="store_true", help="覆盖远端现有文件")

    for action in ("start", "stop", "status"):
        control = commands.add_parser(action, help=f"{action} Manager 云主机或 Monitor 容器")
        control.add_argument("target", choices=("manager", "monitor"))
        if action == "stop":
            control.add_argument("--force", action="store_true", help="停服失败时仍关闭 Manager")

    manager = commands.add_parser("manager", help="调用远端 Manager")
    manager_commands = manager.add_subparsers(dest="action", required=True)
    manager_create = manager_commands.add_parser("create")
    manager_create.add_argument("pack_id")
    manager_create.add_argument(
        "--vanilla", nargs="?", const="latest", metavar="VERSION",
        help="在 Manager 上下载最新正式版或指定版本的 Mojang 原版服务端",
    )
    manager_create.add_argument(
        "--accept-eula", action="store_true",
        help="确认接受 Minecraft EULA",
    )
    manager_create.add_argument(
        "--force", action="store_true",
        help="完整替换远端已存在的同名整合包",
    )
    for action in ("config", "start", "stop", "status", "backup"):
        item = manager_commands.add_parser(action)
        item.add_argument("pack_id")
    logs = manager_commands.add_parser("logs")
    logs.add_argument("pack_id")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    console = manager_commands.add_parser("console")
    console.add_argument("pack_id")
    console.add_argument("command", nargs=argparse.REMAINDER)
    restore = manager_commands.add_parser("restore")
    restore.add_argument("pack_id")
    restore.add_argument("backup", nargs="?")
    remove = manager_commands.add_parser("remove")
    remove.add_argument("pack_id")
    remove.add_argument("--force", action="store_true")
    manager_commands.add_parser("stop-all")

    local = commands.add_parser("local", help="调用本机运行时")
    local_runtime = local.add_subparsers(dest="runtime", required=True)
    local_manager = local_runtime.add_parser("manager")
    local_manager_commands = local_manager.add_subparsers(dest="action", required=True)
    local_create = local_manager_commands.add_parser("create")
    local_create.add_argument("pack_id")
    local_create.add_argument(
        "--vanilla", nargs="?", const="latest", metavar="VERSION",
        help="在本机 Manager 下载最新正式版或指定版本的 Mojang 原版服务端",
    )
    local_create.add_argument(
        "--accept-eula", action="store_true",
        help="确认接受 Minecraft EULA",
    )
    local_create.add_argument(
        "--force", action="store_true",
        help="完整替换本机已存在的同名整合包",
    )

    monitor = commands.add_parser("monitor", help="调用远端 Monitor")
    monitor_commands = monitor.add_subparsers(dest="action", required=True)
    for action in ("start", "stop", "status"):
        monitor_commands.add_parser(action)
    add_target = monitor_commands.add_parser("add-target", help="验证并加入 QQ 单聊目标")
    add_target.add_argument("name", nargs="?", default="owner")
    return parser


def _check_config(path: Path) -> None:
    loaded = load(path)
    configured = (loaded.manager.power_api, loaded.monitor.power_api)
    for settings in configured:
        if settings:
            load_provider(settings.provider)
    monitor = "启用" if loaded.monitor.enabled else "关闭"
    print(f"配置有效：manager={loaded.manager.endpoint.host}，monitor={monitor}")


def _dispatch_manager(args, services: CliServices) -> int:
    pack_id = getattr(args, "pack_id", None)
    extra = []
    tty = False
    if args.action == "logs":
        if args.lines < 0:
            raise ManagerError("日志行数不能为负数")
        extra.extend(("--lines", str(args.lines)))
        if args.follow:
            extra.append("--follow")
            tty = True
    elif args.action == "console":
        if not args.command:
            raise ManagerError("控制台命令不能为空")
        extra.extend(args.command)
    elif args.action == "create":
        if args.vanilla is not None:
            extra.extend(("--vanilla", args.vanilla))
        if args.accept_eula:
            extra.append("--accept-eula")
        if args.force:
            extra.append("--force")
    elif args.action == "restore" and args.backup:
        extra.append(args.backup)
    elif args.action == "remove" and args.force:
        extra.append("--force")
    services.manager_command(
        args.action, pack_id, extra=tuple(extra), tty=tty,
    )
    return 0


def dispatch(argv: Optional[list[str]] = None, service: Optional[CliServices] = None) -> int:
    source_mode = getattr(service, "source_mode", SOURCE_MODE)
    args = make_parser(source_mode=source_mode).parse_args(argv)
    if args.module == "config":
        if args.command == "init":
            for path, created in initialize(args.config):
                action = "已创建" if created else "已存在，未覆盖"
                print(f"{action}：{path}")
            return 0
        _check_config(args.path or args.config)
        return 0

    services = service or CliServices(args.config)
    if args.module == "deploy":
        if args.deploy_kind is None:
            action = services.check if args.test_all else services.install
            label = "部署检查通过" if args.test_all else "部署完成"
            for target in services.deployment_targets():
                action(target)
                print(f"{label}：{target}")
            return 0
        if args.test_all:
            raise DeployError("--test-all 不能与部署子命令同时使用")
        if args.deploy_kind in ("manager", "monitor"):
            if args.sync_config:
                if args.key is not None:
                    raise DeployError("--key 只能与 --create-user 同时使用")
                services.configure(args.deploy_kind)
                print(f"配置已同步并应用：{args.deploy_kind}")
                return 0
            if getattr(args, "install_python", False):
                if args.key is not None:
                    raise DeployError("--key 只能与 --create-user 同时使用")
                services.install_python()
                print("Manager Python 环境已就绪")
                return 0
            if args.install_docker:
                if args.key is not None:
                    raise DeployError("--key 只能与 --create-user 同时使用")
                services.install_docker(args.deploy_kind)
                print(f"Docker 已就绪：{args.deploy_kind}")
                return 0
            if args.source:
                if args.key is not None:
                    raise DeployError("--key 只能与 --create-user 同时使用")
                services.publish(args.deploy_kind)
                print(f"运行时程序文件已同步：{args.deploy_kind}")
                return 0
            if args.create_user:
                try:
                    administrator = input(
                        f"{args.deploy_kind.title()} 服务器现有管理账号 [root]："
                    ).strip() or "root"
                except EOFError as exc:
                    raise AccountError("无法读取管理账号") from exc
                host, public_key = services.account_context(
                    args.deploy_kind, args.key,
                )
                private_key = public_key.with_suffix("")
                print(
                    f"使用 {private_key} 私钥测试登录 "
                    f"{administrator}@{host}"
                )
                services.test_account_login(host, administrator, public_key)
                try:
                    username = input("要创建的用户名：").strip()
                except EOFError as exc:
                    raise AccountError("无法读取要创建的用户名") from exc
                print("将执行：")
                print(f"- 在 {host} 创建或复用用户 {username}")
                print(f"- 将 {public_key} 写入该用户的 authorized_keys")
                print("- 锁定新用户密码并授予免密 sudo")
                print("- 若存在 docker 组则加入，并验证远端账号")
                services.create_user(host, username, administrator, public_key)
                print(f"部署用户已就绪：{username}@{host}")
                return 0
            if args.key is not None:
                raise DeployError("--key 只能与 --create-user 同时使用")
            if args.test:
                services.check(args.deploy_kind)
                print(f"部署检查通过：{args.deploy_kind}")
            else:
                services.install(args.deploy_kind)
                print(f"部署完成：{args.deploy_kind}")
            return 0
        services.deploy_modpack(args.pack_id, force=args.force)
        print(f"整合包已部署：{args.pack_id}")
        return 0

    if args.module in ("start", "stop", "status"):
        force = getattr(args, "force", False)
        if args.module == "stop" and args.target == "manager" and not force:
            try:
                answer = input("将停止全部整合包并关闭 Manager 云主机；输入 yes 确认：")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                print("已取消关机")
                return 0
        result = services.control(args.target, args.module, force=force)
        if result is None:
            print(f"Monitor 容器操作已提交：{args.module}")
        else:
            suffix = f"，公网 IP={result.public_ip}" if result.public_ip else ""
            print(f"Manager 云主机：{result.state}{suffix}")
        return 0

    if args.module == "manager":
        return _dispatch_manager(args, services)
    if args.module == "local":
        extra = []
        if args.vanilla is not None:
            extra.extend(("--vanilla", args.vanilla))
        if args.accept_eula:
            extra.append("--accept-eula")
        if args.force:
            extra.append("--force")
        path = services.local_create(args.pack_id, extra=tuple(extra))
        print(f"已创建本地整合包：{path}")
        if args.vanilla is None:
            print(f"请覆盖 src/ 内容：{path / 'src'}")
            print(f"请调整 config.yml 配置：{path / 'config.yml'}")
        return 0
    if args.module == "monitor":
        if args.action == "add-target":
            identifier = services.add_monitor_target(args.name)
            print(f"已更新 Monitor QQ 目标：{args.name} ({identifier})")
        else:
            services.monitor_command(args.action)
        return 0
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return dispatch(argv)
    except (
        AccountError, CommandError, ConfigError, DeployError, ManagerError, PowerError,
        ProviderError, OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
