"""MCSP Terminal：解析命令并编排 Manager、Monitor 与电源接口。"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import IO, Iterator, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import (
    CONFIG_FILE, ROOT, ConfigError, deployment_connection,
    deployment_host, initialize, load, load_monitor,
)
from .deploy import (
    AccountError, AccountProvisioner, DeployError,
)
from .docker import DockerProvisioner
from .manager import Manager, ManagerError
from .manager_sdk import ManagerSdkError
from .monitor import Monitor
from .power import InstanceStatus, PowerError
from .power import ProviderError, load_provider
from .remote import CommandError, Transport, shell_join


SOURCE_MODE = not getattr(sys, "frozen", False)
DISPLAY_TIMEZONE = ZoneInfo("Asia/Shanghai")
TERMINAL_LOG = ROOT / "terminal.log"
_ACTIVE_LOG: Path | None = None


class _TeeBuffer:
    def __init__(self, original, log: IO[str]) -> None:
        self.original = original
        self.log = log

    def write(self, value: bytes) -> int:
        written = self.original.write(value)
        self.log.write(value.decode("utf-8", "replace"))
        self.log.flush()
        return written

    def flush(self) -> None:
        self.original.flush()
        self.log.flush()


class _Tee:
    """在不改变终端行为的同时记录普通 print 与 buffer 输出。"""

    def __init__(self, original, log: IO[str]) -> None:
        self.original = original
        self.log = log
        self.buffer = _TeeBuffer(original.buffer, log)
        self.encoding = original.encoding
        self.errors = original.errors

    def write(self, value: str) -> int:
        written = self.original.write(value)
        self.log.write(value)
        self.log.flush()
        return written

    def flush(self) -> None:
        self.original.flush()
        self.log.flush()

    def isatty(self) -> bool:
        return self.original.isatty()

    def fileno(self) -> int:
        return self.original.fileno()


def _append_operation(source: Path) -> None:
    TERMINAL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, TERMINAL_LOG.open("ab") as output:
        try:
            import fcntl
            fcntl.flock(output.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        shutil.copyfileobj(incoming, output)
        output.flush()
        os.fsync(output.fileno())


@contextlib.contextmanager
def operation_log(arguments: list[str]) -> Iterator[None]:
    """每次调用独立记录，结束后整块合并，防止并发日志交叉。"""
    global _ACTIVE_LOG
    directory = ROOT / "tmp/terminal-logs"
    directory.mkdir(parents=True, exist_ok=True)
    operation_id = (
        f"{datetime.now(DISPLAY_TIMEZONE):%Y%m%d-%H%M%S}-"
        f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    )
    temporary = directory / f"{operation_id}.log"
    previous = _ACTIVE_LOG
    _ACTIVE_LOG = temporary
    started = time.monotonic()
    try:
        with temporary.open("a", encoding="utf-8") as detail:
            detail.write(
                f"\n=== operation {operation_id} ===\n"
                f"command: mcsp {shell_join(arguments)}\n"
            )
            detail.flush()
            original_stdout, original_stderr = sys.stdout, sys.stderr
            sys.stdout = _Tee(original_stdout, detail)
            sys.stderr = _Tee(original_stderr, detail)
            try:
                yield
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                sys.stdout, sys.stderr = original_stdout, original_stderr
                detail.write(f"duration: {time.monotonic() - started:.3f}s\n")
                detail.write(f"=== end operation {operation_id} ===\n")
        _append_operation(temporary)
    finally:
        _ACTIVE_LOG = previous
        temporary.unlink(missing_ok=True)


def progress(message: str) -> None:
    print(f"[{datetime.now(DISPLAY_TIMEZONE):%H:%M:%S}] {message}", flush=True)


def audit(message: str) -> None:
    path = _ACTIVE_LOG or TERMINAL_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(
            f"[{datetime.now(DISPLAY_TIMEZONE).isoformat(timespec='seconds')}] {message}\n"
        )


class CliServices:
    """Terminal 只编排；实际操作全部委托给运行时或 Provider。"""

    def __init__(
        self, config_path: Path = CONFIG_FILE, *, source_mode: bool = SOURCE_MODE,
    ) -> None:
        self.config_path = config_path
        self.source_mode = source_mode
        self.transport = Transport(_ACTIVE_LOG or TERMINAL_LOG)
        self.accounts = AccountProvisioner(self.transport)
        self.manager: Optional[Manager] = None
        self._monitor_instance: Optional[Monitor] = None

    def manager_runtime(self) -> Manager:
        if self.manager is None:
            progress(f"[配置] 加载 {self.config_path}")
            self.manager = Manager(self.config_path, transport=self.transport)
        return self.manager

    def monitor(self) -> Monitor:
        if self._monitor_instance is None:
            progress(f"[配置] 加载 {self.config_path}")
            self._monitor_instance = Monitor(
                self.config_path, transport=self.transport,
            )
        return self._monitor_instance

    def check(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).check()

    def install(self, target: str) -> None:
        self.install_docker(target)
        (self.manager_runtime() if target == "manager" else self.monitor()).install()

    def publish(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).publish()

    def configure(self, target: str) -> None:
        (self.manager_runtime() if target == "manager" else self.monitor()).configure()

    def deploy_modpack(self, pack_id: str) -> None:
        self.manager_runtime().deploy_modpack(pack_id)

    def export_modpack(self, pack_id: str, destination: Path, **options) -> None:
        self.manager_runtime().export_archive(pack_id, destination, **options)

    def import_modpack(self, archive: Path, *, force: bool) -> None:
        self.manager_runtime().import_archive(archive, force=force)

    def install_docker(self, target: str) -> None:
        progress(f"[配置] 读取 {target} 部署连接：{self.config_path}")
        host, user = deployment_connection(self.config_path, target)
        DockerProvisioner(host, user, transport=self.transport).install()

    def local_manager(self, arguments: list[str]) -> None:
        self.manager_runtime().local(arguments)

    def manager_command(
        self, action: str, pack_id: Optional[str] = None, *,
        extra: Tuple[str, ...] = (), tty: bool = False,
    ) -> None:
        self.manager_runtime().run(action, pack_id, extra=extra, tty=tty)

    def monitor_command(self, action: str) -> None:
        self.monitor().run(action)

    def add_monitor_admin(self) -> str:
        return self.monitor().add_admin()

    def reset_monitor(self) -> None:
        self.monitor().reset()

    def reset_monitor_messages(self) -> None:
        self.monitor().reset_messages()


    def account_context(
        self, target: str, key_path: Optional[Path] = None,
    ) -> tuple[str, Path]:
        progress(f"[配置] 读取 {target} 部署主机：{self.config_path}")
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
        progress(f"[配置] 读取已启用部署目标：{self.config_path}")
        targets = ["manager"]
        if load_monitor(self.config_path).enabled:
            targets.append("monitor")
        return tuple(targets)

    def control(
        self, target: str, action: str, *, force: bool = False,
    ) -> Optional[InstanceStatus]:
        if target == "monitor":
            monitor = self.monitor()
            progress("[容器 1/2] 连接 Monitor 服务器")
            progress(f"[容器 2/2] 执行 Monitor {action}")
            monitor.run(action)
            return None
        total = 2 if action == "status" else (3 if action == "start" else 4)
        manager = self.manager_runtime()
        progress(f"[电源 1/{total}] 加载 Manager 电源 Provider")
        power = manager.power()
        progress(f"[电源 2/{total}] 查询 Manager 云主机状态")
        current = power.status()
        address = f"，公网 IP={current.public_ip}" if current.public_ip else ""
        if action == "status":
            return current
        progress(f"[电源 2/{total}] 当前状态：{current.state}{address}")
        if action == "start":
            if current.state == "RUNNING":
                progress("[电源 3/3] 云主机已运行，无需重复开机")
            else:
                progress("[电源 3/3] 提交开机并等待 RUNNING")
            return power.start(current)
        if current.state == "RUNNING":
            progress("[电源 3/4] 停止全部运行中的整合包")
            try:
                manager.run("stop-all")
            except CommandError:
                if not force:
                    raise
        else:
            progress(f"[电源 3/4] 当前为 {current.state}，无需停服")
        progress("[电源 4/4] 提交关机并等待 STOPPED")
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
        runtime.add_argument(
            "--key", type=Path, help="创建用户所用 SSH 公钥；默认自动寻找",
        )
    deploy_pack = deploy_commands.add_parser("modpack", help="上传整合包")
    deploy_pack.add_argument("pack_id")

    for action in ("start", "stop", "status"):
        control = commands.add_parser(action, help=f"{action} Manager 云主机或 Monitor 容器")
        control.add_argument("target", choices=("manager", "monitor"))
        if action == "stop":
            control.add_argument("--force", action="store_true", help="停服失败时仍关闭 Manager")

    manager = commands.add_parser("manager", help="调用远端 Manager")
    manager_commands = manager.add_subparsers(dest="action", required=True)
    manager_commands.add_parser("list", help="列出远端整合包")
    manager_create = manager_commands.add_parser("create")
    manager_create.add_argument("pack_id")
    manager_create.add_argument(
        "--force", action="store_true",
        help="完整替换远端已存在的同名整合包",
    )
    for action in ("config", "stop", "status", "enable", "disable", "remove-container"):
        item = manager_commands.add_parser(action)
        item.add_argument("pack_id")
        if action in ("enable", "disable"):
            item.add_argument("--now", action="store_true")
    backup = manager_commands.add_parser("backup", help="创建或列出备份")
    backup.add_argument("target", help="整合包 ID，或 list")
    backup.add_argument("pack_id", nargs="?", help="使用 list 时指定整合包 ID")
    manager_start = manager_commands.add_parser("start")
    manager_start.add_argument("pack_id")
    logs = manager_commands.add_parser("logs")
    logs.add_argument("pack_id")
    logs.add_argument("--lines", type=int, default=100, help="读取末尾 N 行")
    console = manager_commands.add_parser("console")
    console.add_argument("pack_id")
    console.add_argument("command", nargs=argparse.REMAINDER)
    prop = manager_commands.add_parser("property", help="读写服务端属性")
    prop.add_argument("pack_id")
    prop.add_argument("key")
    prop.add_argument("value", nargs="?")
    restore = manager_commands.add_parser("restore")
    restore.add_argument("pack_id")
    restore.add_argument("backup", nargs="?")
    restore.add_argument("--auto-restart", action="store_true")
    remove = manager_commands.add_parser("remove")
    remove.add_argument("pack_id")
    remove.add_argument("--force", action="store_true")
    manager_commands.add_parser("stop-all")
    export = manager_commands.add_parser("export", help="下载标准整合包归档")
    export.add_argument("pack_id"); export.add_argument("destination", type=Path)
    export.add_argument("--source-only", action="store_true")
    export.add_argument("--include-backups", action="store_true")
    export.add_argument("--auto-stop", action="store_true")
    import_command = manager_commands.add_parser("import", help="上传并导入标准整合包归档")
    import_command.add_argument("archive", type=Path); import_command.add_argument("--force", action="store_true")

    local = commands.add_parser("local", help="调用本机运行时")
    local_runtime = local.add_subparsers(dest="runtime", required=True)
    local_manager = local_runtime.add_parser("manager")
    local_manager.add_argument(
        "arguments", nargs=argparse.REMAINDER,
        help="原样传给本机 Manager 入口的参数",
    )

    monitor = commands.add_parser("monitor", help="调用远端 Monitor")
    monitor_commands = monitor.add_subparsers(dest="action", required=True)
    for action in ("start", "stop", "status"):
        monitor_commands.add_parser(action)
    monitor_commands.add_parser("add-admin", help="通过 QQ 私聊验证码添加管理员")
    monitor_commands.add_parser("test-alert", help="向目标发送一条安全测试告警")
    monitor_commands.add_parser("reset-targets", help="清空 QQ 管理员与订阅目标")
    monitor_commands.add_parser(
        "reset-messages", help="备份自定义文案并恢复当前版本默认模板",
    )
    return parser


def _check_config(path: Path) -> None:
    print(f"[配置 1/2] 读取并校验 {path}", flush=True)
    loaded = load(path)
    print("[配置 2/2] 检查电源 Provider", flush=True)
    configured = (loaded.manager.power_api, loaded.monitor.power_api)
    for settings in configured:
        if settings:
            load_provider(settings.provider)
    monitor = "启用" if loaded.monitor.enabled else "关闭"
    print(f"配置有效：manager={loaded.manager.endpoint.host}，monitor={monitor}")


def _dispatch_manager(args, services: CliServices) -> int:
    if args.action == "export":
        services.export_modpack(args.pack_id, args.destination, source_only=args.source_only,
                                include_backups=args.include_backups, auto_stop=args.auto_stop)
        return 0
    if args.action == "import":
        services.import_modpack(args.archive, force=args.force)
        return 0
    pack_id = getattr(args, "pack_id", None)
    extra = []
    if args.action == "logs":
        if args.lines < 0:
            raise ManagerError("日志行数不能为负数")
        extra.extend(("--lines", str(args.lines)))
    elif args.action == "console":
        if not args.command:
            raise ManagerError("控制台命令不能为空")
        extra.extend(args.command)
    elif args.action == "property":
        extra.append(args.key)
        if args.value is not None:
            extra.append(args.value)
    elif args.action == "create":
        if args.force:
            extra.append("--force")
    elif args.action in ("enable", "disable") and args.now:
        extra.append("--now")
    elif args.action == "backup":
        pack_id = args.target
        if args.pack_id is not None:
            extra.append(args.pack_id)
    elif args.action == "restore":
        if args.backup:
            extra.append(args.backup)
        if args.auto_restart:
            extra.append("--auto-restart")
    elif args.action == "remove" and args.force:
        extra.append("--force")
    services.manager_command(args.action, pack_id, extra=tuple(extra))
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
        services.deploy_modpack(args.pack_id)
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
            progress(f"Manager 云主机：{result.state}{suffix}")
        return 0

    if args.module == "manager":
        return _dispatch_manager(args, services)
    if args.module == "local":
        services.local_manager(args.arguments)
        return 0
    if args.module == "monitor":
        if args.action == "add-admin":
            identifier = services.add_monitor_admin()
            print(f"已添加 Monitor QQ 管理员：{identifier}")
        elif args.action == "reset-targets":
            services.reset_monitor()
            print("已重置 Monitor QQ 管理员与订阅目标")
        elif args.action == "reset-messages":
            services.reset_monitor_messages()
            print("已重置 Monitor QQ 消息模板")
        else:
            services.monitor_command(args.action)
        return 0
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    with operation_log(arguments):
        return _main(arguments)


def _main(arguments: list[str]) -> int:
    audit(f"$ mcsp {shell_join(arguments)}")
    try:
        result = dispatch(arguments)
    except SystemExit as exc:
        audit(f"exit={exc.code}")
        raise
    except (
        AccountError, CommandError, ConfigError, DeployError, ManagerError,
        ManagerSdkError, PowerError, ProviderError, OSError,
    ) as exc:
        if not getattr(exc, "reported", False):
            print(f"错误：{exc}", file=sys.stderr)
        audit(f"exit=2 error={exc}")
        return 2
    except KeyboardInterrupt:
        audit("exit=130 interrupted")
        raise
    except Exception as exc:
        audit(f"exit=1 exception={type(exc).__name__}: {exc}")
        raise
    audit(f"exit={result}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
