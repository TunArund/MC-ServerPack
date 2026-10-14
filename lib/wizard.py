"""Interactive first-run workflow for MC-ServerPack."""

from __future__ import annotations

import getpass
import shutil
from pathlib import Path
from typing import Callable

import yaml

from . import modpack
from .config import CONFIG, ROOT
from .controller import Controller, ENV_FILE, MANAGER


class WizardExit(RuntimeError):
    pass


def ask(label: str, default: str = "", *, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip() or default
        if value or not required:
            return value
        print("此项不能为空。")


def confirm(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{hint}]: ").strip().lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("请输入 y 或 n。")


def ask_int(label: str, default: int) -> int:
    while True:
        try:
            return int(ask(label, str(default)))
        except ValueError:
            print("请输入整数。")


def ask_java_versions(current: object = None) -> list[int]:
    default = " ".join(map(str, current)) if isinstance(current, list) and current else "21"
    while True:
        raw = ask("Java 环境（可用 8 17 21，空格分隔）", default)
        try:
            versions = [int(item) for item in raw.replace(",", " ").split()]
        except ValueError:
            versions = []
        if versions and len(versions) == len(set(versions)) and all(item in (8, 17, 21) for item in versions):
            return versions
        print("请从 8、17、21 中选择，不能重复。")


def secret(label: str, current: str = "") -> str:
    while True:
        suffix = "（回车保留现有值）" if current else ""
        value = getpass.getpass(f"{label}{suffix}: ").strip()
        if value:
            return value
        if current:
            return current
        print("此项不能为空。")


def command(*arguments: str) -> None:
    rendered = " ".join(f'"{item}"' if " " in item else item for item in arguments)
    print(f"\n将执行：mc {rendered}")


def section(number: int, title: str) -> None:
    print(f"\n[{number}/6] {title}")


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.is_file():
        for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if raw.strip() and not raw.lstrip().startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def write_env(values: dict[str, str]) -> None:
    if not ENV_FILE.exists():
        shutil.copy2(ROOT / ".env.example", ENV_FILE)
    if not values:
        return
    with ENV_FILE.open("r", encoding="utf-8", newline="") as stream:
        lines = stream.readlines()
    remaining = dict(values)
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if stripped and not stripped.startswith("#") and "=" in stripped else ""
        if key in remaining:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            output.append(f"{key}={remaining.pop(key)}{ending}")
        else:
            output.append(line)
    if remaining:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\n"
        output.extend(f"{key}={value}\n" for key, value in remaining.items())
    with ENV_FILE.open("w", encoding="utf-8", newline="") as stream:
        stream.writelines(output)


def configure_project() -> None:
    current = load_yaml(CONFIG)
    manager = current.get("manager") if isinstance(current.get("manager"), dict) else {}
    user = ask("manager SSH 用户", str(manager.get("user", "steam")))
    host = ask("manager SSH 主机或地址", str(manager.get("host", "")))
    directory = ask(
        "manager 远端绝对路径",
        str(manager.get("dir", f"/home/{user}/MC-ServerPack/manager")),
    )
    java_versions = ask_java_versions(manager.get("java"))
    document: dict = {
        "manager": {
            "host": host, "user": user, "dir": directory, "java": java_versions,
        }
    }
    if manager.get("modpack"):
        document["manager"]["modpack"] = manager["modpack"]
    if isinstance(current.get("cos"), dict):
        document["cos"] = current["cos"]
    if confirm("配置可选的 gateway/QQ Bot（留空跳过）", False):
        old = current.get("gateway") if isinstance(current.get("gateway"), dict) else {}
        gw_user = ask("gateway SSH 用户", str(old.get("user", user)))
        target_type = ask("QQ 目标类型 user/group", "group")
        if target_type not in ("user", "group"):
            raise RuntimeError("QQ 目标类型只能是 user 或 group")
        target = ask("QQ 目标 ID")
        admin = ask("QQ 管理员 ID", target)
        document["gateway"] = {
            "host": ask("gateway SSH 主机或地址", str(old.get("host", ""))),
            "user": gw_user,
            "dir": ask("gateway 远端绝对路径", str(old.get("dir", f"/home/{gw_user}/MC-ServerPack/qqbot"))),
            "region": ask("腾讯云地域", str(old.get("region", "ap-nanjing"))),
            "instance-id": ask("manager 所在云主机实例 ID", str(old.get("instance-id", ""))),
            "schedule": {
                "enabled": confirm("启用自动开关机", True),
                "start": ask("预计可进入服务器时间 HH:MM", "20:00"),
                "start-elast": ask_int("预计启动耗时（分钟）", 5),
                "end": ask("结束游玩时间 HH:MM", "23:00"),
                "end-elast": ask_int("关机宽限（分钟）", 3),
                "notify-elast": ask_int("首次关服提醒（分钟前）", 60),
                "notify-step": ask_int("提醒递减倍数", 2),
            },
            "qqbot": {"targets": [{"type": target_type, "id": target}], "admins": [admin]},
        }
    write_yaml(CONFIG, document)
    print(f"已写入：{CONFIG}")


def configure_secrets() -> None:
    document, current, updates = load_yaml(CONFIG), load_env(), {}
    if "gateway" in document:
        print("安全考虑，输入密钥时不会显示。")
        for key, label in (
            ("GW_SECRET_ID", "腾讯云 CAM SecretId"),
            ("GW_SECRET_KEY", "腾讯云 CAM SecretKey"),
            ("QQBOT_SECRET_ID", "QQ Bot SecretId"),
            ("QQBOT_SECRET_KEY", "QQ Bot SecretKey"),
        ):
            updates[key] = secret(label, current.get(key, ""))
    if "cos" in document:
        print("安全考虑，输入 COS 密钥时不会显示。")
        updates["COS_SECRET_ID"] = secret("COS SecretId", current.get("COS_SECRET_ID", ""))
        updates["COS_SECRET_KEY"] = secret("COS SecretKey", current.get("COS_SECRET_KEY", ""))
    if "gateway" not in document and "cos" not in document:
        print("当前只配置 manager，没有必需密钥；已跳过。")
    write_env(updates)
    print(f"已写入：{ENV_FILE}")


def retry(label: str, action: Callable[[], None]) -> None:
    while True:
        try:
            action()
            return
        except (OSError, RuntimeError) as exc:
            print(f"\n{label}失败：{exc}")
            print("请根据错误检查网络、SSH、权限或配置。")
            choice = input("输入 r 重试，输入 e 退出向导: ").strip().lower()
            if choice != "r":
                raise WizardExit()


def configure_pack() -> str:
    pack = ask("整合包 ID", "my-pack")
    target = MANAGER / "modpacks" / pack
    if target.exists():
        if not confirm(f"{pack} 已存在，继续使用", True):
            raise WizardExit()
    else:
        command("modpack", "create", pack)
        modpack.create(MANAGER, pack)
    while True:
        source = ask("服务端目录、文件、ZIP/TAR 路径或 HTTP(S) URL")
        existing = [item for item in (target / "src").iterdir() if item.name != ".gitkeep"]
        force = bool(existing) and confirm("src/ 已有内容，确认合并导入", False)
        command("modpack", "import", *(('--force',) if force else ()), pack, source)
        try:
            modpack.import_into(MANAGER, pack, source, force)
            break
        except (OSError, RuntimeError) as exc:
            print(f"\n导入整合包失败：{exc}")
            if input("输入 r 重新输入来源，输入 e 退出向导: ").strip().lower() != "r":
                raise WizardExit()

    path = target / "config.yml"
    spec = load_yaml(path)
    spec["name"] = ask("整合包名称", str(spec.get("name", pack)))
    project = load_yaml(CONFIG)
    available = project.get("manager", {}).get("java", [21])
    default_java = spec.get("java") if spec.get("java") in available else available[0]
    while True:
        selected_java = ask_int(f"Java 版本（已构建：{' '.join(map(str, available))}）", int(default_java))
        if selected_java in available:
            spec["java"] = selected_java
            break
        print("请选择已经构建的 Java 环境。")
    spec["memory"] = ask("内存限制", str(spec.get("memory", "4g")))
    current = spec.get("dynamic-paths", [])
    if not isinstance(current, list):
        current = []
    print("当前动态路径：" + ", ".join(map(str, current)))
    additions = ask("追加动态路径（逗号分隔，直接回车跳过）", required=False)
    if additions:
        for item in (part.strip() for part in additions.split(",")):
            if item and item not in current:
                current.append(item)
    spec["dynamic-paths"] = current
    write_yaml(path, spec)
    print(f"已写入：{path}")
    return pack


def run() -> int:
    print("\nMC-ServerPack 快速配置向导。按 Ctrl+C 可随时退出。")
    try:
        section(1, "配置 manager")
        print("仅 SSH 连接和 Java 环境为必填；gateway 可选，留空默认不配置。")
        configure_project()

        section(2, "填写密钥")
        configure_secrets()

        section(3, "部署 manager")
        command("deploy", "manager")
        retry("部署 manager", lambda: Controller().deploy_manager())

        section(4, "创建并配置整合包")
        pack = configure_pack()

        section(5, "上传整合包")
        command("modpack", "deploy", pack)
        retry("上传整合包", lambda: Controller().deploy_modpack(pack))

        section(6, "启动、状态和日志")
        controller = Controller()
        for arguments in (("start", pack), ("status", pack), ("logs", pack)):
            command("manager", *arguments)
            retry(f"manager {arguments[0]}", lambda args=list(arguments): controller.remote_manager(args))
        print("\n快速配置完成。以后直接使用 manager、modpack、gateway 等模块管理服务器。")
        return 0
    except (KeyboardInterrupt, EOFError, WizardExit):
        print("\n已退出向导；已完成的配置和部署不会回滚。")
        return 1
