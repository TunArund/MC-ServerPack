"""Manager-host command-line interface."""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError
from .docker import DockerError
from .modpacks import ModpackError, ModpackManager


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manager.sh", description="管理本机 Minecraft 整合包")
    commands = parser.add_subparsers(dest="module", required=True)
    pack = commands.add_parser("pack", help="整合包生命周期")
    pack.add_argument("pack_id", help="整合包 ID，或 stop-all")
    actions = pack.add_subparsers(dest="action")
    for action in ("create", "config", "start", "stop", "status"):
        actions.add_parser(action)
    logs = actions.add_parser("logs")
    logs.add_argument("--lines", type=int, default=100, help="日志行数")
    logs.add_argument("--follow", action="store_true", help="持续跟随日志")
    console = actions.add_parser("console")
    console.add_argument("command", nargs=argparse.REMAINDER, help="console 命令")
    return parser


def dispatch(argv: list[str] | None = None, manager: ModpackManager | None = None) -> int:
    args = make_parser().parse_args(argv)
    service = manager or ModpackManager()
    if args.pack_id == "stop-all":
        if args.action:
            raise ModpackError("stop-all 不接受其他参数")
        stopped, failed = service.stop_all()
        for pack_id in stopped:
            print(f"已停服：{pack_id}")
        for pack_id, reason in failed:
            print(f"停服失败：{pack_id}：{reason}", file=sys.stderr)
        return 1 if failed else 0
    if not args.action:
        raise ModpackError("缺少整合包操作")
    if args.action == "logs" and args.lines < 0:
        raise ModpackError("日志行数不能为负数")
    if args.action == "create":
        print(f"已创建：{service.create(args.pack_id)}")
    elif args.action == "config":
        print(f"配置有效：{service.configure(args.pack_id)}")
    elif args.action == "start":
        service.start(args.pack_id)
        print(f"已启动：{args.pack_id}")
    elif args.action == "stop":
        service.stop(args.pack_id)
        print(f"已停止：{args.pack_id}")
    elif args.action == "status":
        print(service.status(args.pack_id), end="")
    elif args.action == "logs":
        service.logs(args.pack_id, lines=args.lines, follow=args.follow)
    elif args.action == "console":
        service.console(args.pack_id, " ".join(args.command))
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (ConfigError, DockerError, ModpackError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
