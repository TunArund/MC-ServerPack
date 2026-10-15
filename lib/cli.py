"""Local command-line entry point.

Only commands backed by implemented behavior are exposed.  Adding placeholder
commands here makes an unfinished operation look successful, so later slices
should register a command only when its service implementation exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cloud import CloudError
from .config import CONFIG_FILE, ConfigError, load
from .remote import CommandError
from .terminal import TerminalError, TerminalService


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


def dispatch(argv: list[str] | None = None, service: TerminalService | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.module == "config" and args.command == "check":
        loaded = load(args.path or args.config)
        monitor = "启用" if loaded.monitor.enabled else "关闭"
        print(f"配置有效：manager={loaded.manager.endpoint.host}，monitor={monitor}")
        return 0
    terminal = service or TerminalService(args.config)
    if args.module == "deploy":
        targets = ("manager", "monitor") if args.target == "all" else (args.target,)
        for target in targets:
            if args.check:
                terminal.check(target)
                print(f"远端环境有效：{target}")
            elif target == "manager":
                terminal.deploy_manager()
                print("Manager 部署完成")
            else:
                terminal.deploy_monitor()
                print("Monitor 部署完成")
        return 0
    if args.module == "manager":
        cloud = terminal.cloud()
        if args.action == "status":
            result = cloud.status()
        elif args.action == "start":
            result = cloud.start()
        else:
            if not args.force:
                try:
                    answer = input("将停止全部整合包并关闭云主机；输入 yes 确认：")
                except EOFError:
                    answer = ""
                if answer.strip().lower() != "yes":
                    print("已取消关机")
                    return 0
            result = cloud.stop(continue_on_stop_failure=args.force)
        suffix = f"，公网 IP={result.public_ip}" if result.public_ip else ""
        print(f"Manager 云主机：{result.state}{suffix}")
        return 0
    if args.module == "pack":
        if args.action == "create":
            path = terminal.create_pack(args.pack_id)
            print(f"已创建本地整合包：{path}")
            return 0
        if args.action == "deploy":
            terminal.deploy_pack(args.pack_id, force=args.force)
            print(f"整合包已部署：{args.pack_id}")
            return 0
        arguments = [args.action]
        if hasattr(args, "pack_id"):
            arguments.append(args.pack_id)
        tty = False
        if args.action == "logs":
            if args.lines < 0:
                raise TerminalError("日志行数不能为负数")
            arguments.extend(("--lines", str(args.lines)))
            if args.follow:
                arguments.append("--follow")
                tty = True
        elif args.action == "console":
            if not args.command:
                raise TerminalError("控制台命令不能为空")
            arguments.extend(args.command)
        elif args.action == "restore" and args.backup:
            arguments.append(args.backup)
        elif args.action == "remove" and args.force:
            arguments.append("--force")
        terminal.remote_manager(arguments, tty=tty)
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (CloudError, CommandError, ConfigError, TerminalError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
