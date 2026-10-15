"""Manager-host command-line interface."""

from __future__ import annotations

import argparse
import sys

from .archive import ArchiveError
from .config import ConfigError
from .docker import DockerError
from .modpacks import ModpackError, ModpackManager


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manager.sh", description="管理本机 Minecraft 整合包")
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("create", "config", "start", "stop", "status", "backup"):
        command = commands.add_parser(action)
        command.add_argument("pack_id", help="整合包 ID")
    logs = commands.add_parser("logs")
    logs.add_argument("pack_id")
    logs.add_argument("--lines", type=int, default=100, help="日志行数")
    logs.add_argument("--follow", action="store_true", help="持续跟随日志")
    console = commands.add_parser("console")
    console.add_argument("pack_id")
    console.add_argument("command", nargs=argparse.REMAINDER, help="console 命令")
    restore = commands.add_parser("restore")
    restore.add_argument("pack_id")
    restore.add_argument("backup", nargs="?", help="备份文件名；默认使用最新备份")
    remove = commands.add_parser("remove")
    remove.add_argument("pack_id")
    remove.add_argument("--force", action="store_true", help="确认删除持久化数据和备份")
    commands.add_parser("stop-all")
    return parser


def dispatch(argv: list[str] | None = None, manager: ModpackManager | None = None) -> int:
    args = make_parser().parse_args(argv)
    service = manager or ModpackManager()
    if args.action == "stop-all":
        stopped, failed = service.stop_all()
        for pack_id in stopped:
            print(f"已停服：{pack_id}")
        for pack_id, reason in failed:
            print(f"停服失败：{pack_id}：{reason}", file=sys.stderr)
        return 1 if failed else 0
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
    elif args.action == "backup":
        print(f"备份完成：{service.backup(args.pack_id)}")
    elif args.action == "restore":
        print(f"恢复完成：{service.restore(args.pack_id, args.backup)}")
    elif args.action == "remove":
        service.remove(args.pack_id, force=args.force)
        print(f"已删除：{args.pack_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (ArchiveError, ConfigError, DockerError, ModpackError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
