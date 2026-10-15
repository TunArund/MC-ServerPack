"""Local command-line entry point.

Only commands backed by implemented behavior are exposed.  Adding placeholder
commands here makes an unfinished operation look successful, so later slices
should register a command only when its service implementation exists.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import CONFIG_FILE, ConfigError, load


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcsp", description="Minecraft 服务端部署与管理")
    commands = parser.add_subparsers(dest="module", required=True)
    config = commands.add_parser("config", help="检查本地配置")
    config_commands = config.add_subparsers(dest="command", required=True)
    check = config_commands.add_parser("check", help="验证 config.yml")
    check.add_argument("path", nargs="?", type=Path, default=CONFIG_FILE)
    return parser


def dispatch(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    if args.module == "config" and args.command == "check":
        loaded = load(args.path)
        monitor = "启用" if loaded.monitor.enabled else "关闭"
        print(f"配置有效：manager={loaded.manager.endpoint.host}，monitor={monitor}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (ConfigError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
