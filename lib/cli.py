"""Cross-platform command-line core for MC-ServerPack."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from . import config, modpack
from .controller import Controller, GATEWAY, MANAGER, load_env
from .remote import CommandError, run


def make_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mc", description="MC-ServerPack 本地部署与远程管理入口")
    commands = root.add_subparsers(dest="module", required=True)
    deploy = commands.add_parser("deploy", help="部署 manager 或 gateway")
    deploy.add_argument("target", choices=("manager", "gateway", "all"))
    deploy.add_argument("--check", action="store_true", help="只检查本机配置与远端环境")
    cfg = commands.add_parser("config", help="验证生成的运行配置")
    cfg.add_argument("target", choices=("gateway",))
    pack = commands.add_parser("modpack", help="在本机准备和上传整合包")
    pack_commands = pack.add_subparsers(dest="command", required=True)
    create = pack_commands.add_parser("create"); create.add_argument("id")
    imported = pack_commands.add_parser("import")
    imported.add_argument("id"); imported.add_argument("source"); imported.add_argument("--force", action="store_true")
    scan = pack_commands.add_parser("scan"); scan.add_argument("id")
    upload = pack_commands.add_parser("deploy"); upload.add_argument("id")
    manager = commands.add_parser("manager", help="通过 SSH 调用远端 manager.sh")
    manager.add_argument("arguments", nargs=argparse.REMAINDER, metavar="COMMAND")
    gateway = commands.add_parser("gateway", help="控制云主机电源")
    gateway.add_argument("command", choices=("start", "stop", "status", "guard"))
    bot = commands.add_parser("bot", help="管理 gateway 上的 QQ Bot")
    bot.add_argument("command", choices=("status", "restart", "stop", "logs", "follow"))
    bot.add_argument("lines", nargs="?", default=100, type=int)
    cos = commands.add_parser("cos", help="生成 COS 临时上传或下载签名")
    cos.add_argument("arguments", nargs=argparse.REMAINDER, metavar="ARG")
    commands.add_parser("tui", help="进入 mc> 交互模式")
    return root


def validate_gateway(controller: Controller) -> None:
    values = controller.gateway_env()
    with tempfile.TemporaryDirectory(prefix="mc-serverpack-") as temp:
        output = Path(temp) / "gateway.env"
        output.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        run(["docker", "compose", "--env-file", str(output), "-f", str(GATEWAY / "compose.yml"), "config", "--quiet"])
    print("gateway Compose 配置有效")


def run_cos(arguments: list[str], controller: Controller) -> None:
    secrets, cos = load_env(), controller.section("cos")
    environment = os.environ.copy()
    environment.update({
        "TENCENTCLOUD_SECRET_ID": secrets.get("COS_SECRET_ID", ""),
        "TENCENTCLOUD_SECRET_KEY": secrets.get("COS_SECRET_KEY", ""),
        "REGION": str(cos["region"]), "COS_BUCKET": str(cos["bucket"]),
        "COS_UPLOAD_PREFIX": str(cos["prefix"]), "COS_OBJECT_NAME": secrets.get("COS_OBJECT_NAME", ""),
        "COS_URL_EXPIRE": str(cos["expire"]),
    })
    from .cos import main as cos_main

    previous = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(environment)
        cos_main(arguments)
    finally:
        os.environ.clear()
        os.environ.update(previous)


def dispatch(argv: list[str] | None = None, *, interactive: bool = False) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    if args.module == "tui":
        if interactive:
            raise RuntimeError("已经处于交互模式")
        from .tui import run as run_tui
        return run_tui(dispatch)
    if args.module == "modpack" and args.command == "create":
        modpack.create(MANAGER, args.id); return 0
    if args.module == "modpack" and args.command == "import":
        modpack.import_into(MANAGER, args.id, args.source, args.force); return 0
    if args.module == "modpack" and args.command == "scan":
        modpack.scan(MANAGER, args.id); return 0
    controller = Controller()
    if args.module == "deploy":
        if args.check:
            return controller.check(args.target)
        if args.target in ("manager", "all"):
            controller.deploy_manager()
        if args.target in ("gateway", "all"):
            controller.deploy_gateway()
    elif args.module == "config":
        validate_gateway(controller)
    elif args.module == "modpack":
        controller.deploy_modpack(args.id)
    elif args.module == "manager":
        if not args.arguments:
            parser.error("manager 后需要 manager.sh 子命令")
        controller.remote_manager(args.arguments)
    elif args.module == "gateway":
        controller.gateway(args.command)
    elif args.module == "bot":
        if args.lines < 0:
            raise RuntimeError("日志行数不能为负数")
        controller.bot(args.command, args.lines)
    elif args.module == "cos":
        run_cos(args.arguments, controller)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dispatch(argv)
    except (config.ConfigError, CommandError, OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
