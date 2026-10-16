"""Local command-line entry point.

Only commands backed by implemented behavior are exposed.  Adding placeholder
commands here makes an unfinished operation look successful, so later slices
should register a command only when its service implementation exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import CONFIG_FILE, AppConfig, ConfigError, load, load_env
from .deploy import DeployError, DeploymentService
from .modpacks import ModpackError, ModpackService, validate_pack_id
from .power import PowerError, PowerTarget, TencentPower, WebhookPower
from .remote import CommandError, Transport
from .tencent import ApiCall, TencentApiError, create_api


class CliServices:
    """Wire CLI commands to focused services without owning domain behavior."""

    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        self.config_path = config_path
        self.config: AppConfig = load(config_path)
        self.transport = Transport()
        self.deployment = DeploymentService(
            config_path, config=self.config, transport=self.transport,
        )
        self.modpacks = ModpackService(self.config, transport=self.transport)
        self._api_call: ApiCall | None = None

    def check(self, target: str) -> None:
        self.deployment.check(target)

    def deploy_manager(self) -> None:
        self.deployment.deploy_manager()

    def deploy_monitor(self) -> None:
        self.deployment.deploy_monitor()

    def deploy_pack(self, pack_id: str, *, force: bool = False) -> None:
        self.modpacks.deploy_pack(pack_id, force=force)

    def create_pack(self, pack_id: str) -> Path:
        return self.modpacks.create_pack(pack_id)

    def remote_manager(self, arguments: list[str], *, tty: bool = False) -> None:
        self.modpacks.remote_manager(arguments, tty=tty)

    def _cloud_api(self) -> ApiCall:
        if self._api_call is None:
            power = self.config.manager.power_api
            if not power or power.provider != "tencent":
                raise PowerError("manager.power-api 未配置腾讯云 provider")
            values = load_env(self.config_path.parent / ".env")
            self._api_call = create_api(
                power.instance_region,
                values.get("POWER_SECRET_ID", ""),
                values.get("POWER_SECRET_KEY", ""),
            )
        return self._api_call

    def power(self) -> TencentPower | WebhookPower:
        power = self.config.manager.power_api
        if not power:
            raise PowerError("manager.power-api 未配置")
        target = PowerTarget(power.instance_id, power.instance_region)
        if power.provider == "tencent":
            return TencentPower(target, self._cloud_api())
        values = load_env(self.config_path.parent / ".env")
        return WebhookPower(
            target, power.url or "", values.get("POWER_API_TOKEN", ""),
        )

    def stop_modpacks(self) -> None:
        self.modpacks.remote_manager(["stop-all"])


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
        power = services.power()
        if args.action == "status":
            result = power.status()
        elif args.action == "start":
            result = power.start()
        else:
            if not args.force:
                try:
                    answer = input("将停止全部整合包并关闭云主机；输入 yes 确认：")
                except EOFError:
                    answer = ""
                if answer.strip().lower() != "yes":
                    print("已取消关机")
                    return 0
            current = power.status()
            if current.state == "RUNNING":
                try:
                    services.stop_modpacks()
                except (CommandError, ModpackError, TencentApiError):
                    if not args.force:
                        raise
            result = power.stop(current)
        suffix = f"，公网 IP={result.public_ip}" if result.public_ip else ""
        print(f"Manager 云主机：{result.state}{suffix}")
        return 0
    if args.module == "pack":
        if hasattr(args, "pack_id"):
            validate_pack_id(args.pack_id)
        if args.action == "create":
            path = services.create_pack(args.pack_id)
            print(f"已创建本地整合包：{path}")
            return 0
        if args.action == "deploy":
            services.deploy_pack(args.pack_id, force=args.force)
            print(f"整合包已部署：{args.pack_id}")
            return 0
        arguments = [args.action]
        if hasattr(args, "pack_id"):
            arguments.append(args.pack_id)
        tty = False
        if args.action == "logs":
            if args.lines < 0:
                raise ModpackError("日志行数不能为负数")
            arguments.extend(("--lines", str(args.lines)))
            if args.follow:
                arguments.append("--follow")
                tty = True
        elif args.action == "console":
            if not args.command:
                raise ModpackError("控制台命令不能为空")
            arguments.extend(args.command)
        elif args.action == "restore" and args.backup:
            arguments.append(args.backup)
        elif args.action == "remove" and args.force:
            arguments.append("--force")
        services.remote_manager(arguments, tty=tty)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (
        CommandError, ConfigError, DeployError, ModpackError, PowerError,
        TencentApiError, OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
