"""Local command-line entry point.

Only commands backed by implemented behavior are exposed.  Adding placeholder
commands here makes an unfinished operation look successful, so later slices
should register a command only when its service implementation exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import CONFIG_FILE, AppConfig, ConfigError, load
from .deploy import DeployError
from .manager import ManagerService, ModpackError
from .monitor import MonitorService
from .power import InstanceStatus, PowerError, load_adapter
from .remote import CommandError, Transport


class CliServices:
    """Wire CLI commands to focused services without owning domain behavior."""

    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        self.config_path = config_path
        self.config: AppConfig = load(config_path)
        self.transport = Transport()
        self.manager = ManagerService(
            config_path, config=self.config, transport=self.transport,
        )

    def check(self, target: str) -> None:
        runtime = self.manager if target == "manager" else self._monitor()
        runtime.check()

    def deploy_manager(self) -> None:
        self.manager.deploy()

    def deploy_monitor(self) -> None:
        self._monitor().deploy()

    def deploy_pack(self, pack_id: str, *, force: bool = False) -> None:
        self.manager.deploy_pack(pack_id, force=force)

    def create_pack(self, pack_id: str) -> Path:
        return self.manager.create_pack(pack_id)

    def remote_pack(
        self, action: str, pack_id: str | None = None, *,
        extra: tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        self.manager.remote_pack(action, pack_id, extra=extra, tty=tty)

    def manager_power(
        self, action: str, *, force: bool = False,
    ) -> InstanceStatus:
        return self.manager.power_action(action, force=force)

    def _monitor(self) -> MonitorService:
        return MonitorService(
            self.config_path, config=self.config, transport=self.transport,
        )

    def add_monitor_target(self, name: str) -> str:
        return self._monitor().add_target(name)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcsp", description="Minecraft 服务端部署与管理")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Terminal 配置文件")
    commands = parser.add_subparsers(dest="module", required=True)
    config = commands.add_parser("config", help="检查本地配置")
    config_commands = config.add_subparsers(dest="command", required=True)
    check = config_commands.add_parser("check", help="验证 config.yml")
    check.add_argument("path", nargs="?", type=Path)
    deploy = commands.add_parser("deploy", help="独立部署 Manager 或 Monitor")
    deploy.add_argument("target", choices=("manager", "monitor", "all"))
    deploy.add_argument("--check", action="store_true", help="仅检查 SSH 和远端环境")
    manager = commands.add_parser("manager", help="管理 Manager 云主机电源")
    manager.add_argument("action", choices=("start", "stop", "status"))
    manager.add_argument("--force", action="store_true", help="停服失败时仍提交软关机")
    monitor = commands.add_parser("monitor", help="管理 Monitor 与 QQ Bot")
    monitor_commands = monitor.add_subparsers(dest="action", required=True)
    add_target = monitor_commands.add_parser("add-target", help="验证并加入 QQ 单聊目标")
    add_target.add_argument("name", nargs="?", default="owner")
    pack = commands.add_parser("pack", help="部署或远程管理整合包")
    pack_commands = pack.add_subparsers(dest="action", required=True)
    for action in ("create", "config", "start", "stop", "status", "backup"):
        item = pack_commands.add_parser(action)
        item.add_argument("pack_id")
    upload = pack_commands.add_parser("deploy")
    upload.add_argument("pack_id")
    upload.add_argument("--force", action="store_true", help="覆盖远端 src 和 config.yml")
    logs = pack_commands.add_parser("logs")
    logs.add_argument("pack_id")
    logs.add_argument("--lines", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    console = pack_commands.add_parser("console")
    console.add_argument("pack_id")
    console.add_argument("command", nargs=argparse.REMAINDER)
    restore = pack_commands.add_parser("restore")
    restore.add_argument("pack_id")
    restore.add_argument("backup", nargs="?")
    remove = pack_commands.add_parser("remove")
    remove.add_argument("pack_id")
    remove.add_argument("--force", action="store_true")
    pack_commands.add_parser("stop-all")
    return parser


def dispatch(argv: list[str] | None = None, service: CliServices | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.module == "config" and args.command == "check":
        loaded = load(args.path or args.config)
        configured = [loaded.manager.power_api, loaded.monitor.power_api]
        for power_api in configured:
            if power_api:
                load_adapter(power_api.provider)
        monitor = "启用" if loaded.monitor.enabled else "关闭"
        print(f"配置有效：manager={loaded.manager.endpoint.host}，monitor={monitor}")
        return 0
    services = service or CliServices(args.config)
    if args.module == "deploy":
        targets = ("manager", "monitor") if args.target == "all" else (args.target,)
        for target in targets:
            if args.check:
                services.check(target)
                print(f"远端环境有效：{target}")
            elif target == "manager":
                services.deploy_manager()
                print("Manager 部署完成")
            else:
                services.deploy_monitor()
                print("Monitor 部署完成")
        return 0
    if args.module == "manager":
        if args.action == "stop" and not args.force:
            try:
                answer = input("将停止全部整合包并关闭云主机；输入 yes 确认：")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                print("已取消关机")
                return 0
        result = services.manager_power(args.action, force=args.force)
        suffix = f"，公网 IP={result.public_ip}" if result.public_ip else ""
        print(f"Manager 云主机：{result.state}{suffix}")
        return 0
    if args.module == "monitor" and args.action == "add-target":
        identifier = services.add_monitor_target(args.name)
        print(f"已更新 Monitor QQ 目标：{args.name} ({identifier})")
        return 0
    if args.module == "pack":
        if args.action == "create":
            path = services.create_pack(args.pack_id)
            print(f"已创建本地整合包：{path}")
            return 0
        if args.action == "deploy":
            services.deploy_pack(args.pack_id, force=args.force)
            print(f"整合包已部署：{args.pack_id}")
            return 0
        pack_id = getattr(args, "pack_id", None)
        extra: list[str] = []
        tty = False
        if args.action == "logs":
            if args.lines < 0:
                raise ModpackError("日志行数不能为负数")
            extra.extend(("--lines", str(args.lines)))
            if args.follow:
                extra.append("--follow")
                tty = True
        elif args.action == "console":
            if not args.command:
                raise ModpackError("控制台命令不能为空")
            extra.extend(args.command)
        elif args.action == "restore" and args.backup:
            extra.append(args.backup)
        elif args.action == "remove" and args.force:
            extra.append("--force")
        services.remote_pack(
            args.action, pack_id, extra=tuple(extra), tty=tty,
        )
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (
        CommandError, ConfigError, DeployError, ModpackError, PowerError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
