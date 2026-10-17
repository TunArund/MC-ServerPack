"""
Manager命令接口
import|export <modpack>
create|remove <modpack>
start|stop <modpack>
stop-all
backup|restore <modpack>
logs|status <modpack>
schedule install|run|remove
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backup import BackupError
from .config import ConfigError
from .docker import DockerError, DockerInstaller
from .modpacks import ModpackError, ModpackManager
from .provider import ProviderError, load_provider
from . import schedule
from .transfer import export_pack, import_pack


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manager.sh", description="管理本机 Minecraft 整合包")
    commands = parser.add_subparsers(dest="action", required=True)
    install = commands.add_parser("install", help="安装 Manager 系统组件")
    install.add_argument("component", choices=("docker",))
    create = commands.add_parser("create")
    create.add_argument("pack_id", help="整合包 ID")
    create.add_argument(
        "--force", action="store_true",
        help="完整替换已存在的同名整合包",
    )
    for action in ("config", "stop", "status", "backup"):
        command = commands.add_parser(action)
        command.add_argument("pack_id", help="整合包 ID")
    start = commands.add_parser("start")
    start.add_argument("pack_id", help="整合包 ID")
    start.add_argument(
        "--accept-eula", action="store_true",
        help="确认接受 Minecraft EULA，启动前写入 eula=true",
    )
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
    export = commands.add_parser("export", help="导出可传输的整合包归档")
    export.add_argument("pack_id")
    upload = commands.add_parser("import", help="导入整合包归档")
    upload.add_argument("pack_id")
    upload.add_argument("archive", type=Path)
    upload.add_argument("--force", action="store_true")
    scheduled = commands.add_parser("schedule", help="聚合执行备份、定时停服与云关机")
    scheduled.add_argument("command", choices=("run", "install", "remove"))
    return parser


def dispatch(
    argv: list[str] | None = None, manager: ModpackManager | None = None,
    docker_installer: DockerInstaller | None = None,
) -> int:
    args = make_parser().parse_args(argv)
    if args.action == "install":
        result = (docker_installer or DockerInstaller()).install()
        state = "已存在并验证" if result.already_installed else "安装完成"
        print(f"Docker Engine 与 Compose 插件{state}")
        if result.user_added:
            print(f"已将 {result.user_added} 加入 docker 组；重新登录后生效")
        return 0
    service = manager or ModpackManager()
    if args.action == "export":
        print(f"archive: {export_pack(args.pack_id).resolve()}")
        return 0
    if args.action == "import":
        print(f"已导入：{import_pack(service, args.pack_id, args.archive, force=args.force)}")
        return 0
    if args.action == "schedule":
        if args.command == "install":
            if service.limits.poweroff_enabled:
                load_provider(service.limits.power_provider)
            schedule.install(service.manager_root)
            print("已安装 Manager 用户级调度任务")
            return 0
        if args.command == "remove":
            schedule.remove()
            print("已移除 Manager 用户级调度任务")
            return 0
        report = schedule.run_all(service)
        if not report.acquired:
            print("已有调度任务在运行，本次跳过")
            return 0
        for minutes in report.broadcasts:
            print(f"已广播：距离关服 {minutes} 分钟")
        for pack_id in report.stopped:
            print(f"定时停服完成：{pack_id}")
        for path in report.backups:
            print(f"自动备份完成：{path}")
        if report.poweroff:
            print("已提交 Manager 云主机关机")
        for action, reason in report.failures:
            print(f"调度任务失败：{action}：{reason}", file=sys.stderr)
        if not (report.broadcasts or report.stopped or report.backups or report.poweroff or report.failures):
            print("没有到期的调度任务")
        return 1 if report.failures else 0
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
        created = service.create(args.pack_id, force=args.force)
        print(f"已创建：{created}")
        print(f"请覆盖 src/ 内容：{created / 'src'}")
        print(f"请调整 config.yml 配置：{created / 'config.yml'}")
        print("原版服务端下载：https://www.minecraft.net/download/server")
    elif args.action == "config":
        print(f"配置有效：{service.configure(args.pack_id)}")
    elif args.action == "start":
        service.start(args.pack_id, accept_eula=args.accept_eula)
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
    except (
        BackupError, ConfigError, DockerError, ModpackError, ProviderError,
        schedule.ScheduleError, OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
