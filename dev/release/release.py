#!/usr/bin/env python3
"""MC-ServerPack reproducible release builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RELEASE_ROOT = ROOT / "dev/release"
BUILD_ROOT = RELEASE_ROOT / "build"
DIST_ROOT = RELEASE_ROOT / "dist"
SHARED_FILES = ("manager_sdk.py", "observability.py", "power.py")
SHARED_PROVIDERS = ("example.py", "tencent.py")
COMPONENTS = ("terminal", "manager", "monitor")
EXCLUDED_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".git", ".venv",
    "build", "dist",
}
SECRET_NAMES = {".env", "config.yml"}


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Context:
    version: str
    allow_dirty: bool

    @property
    def output(self) -> Path:
        return DIST_ROOT / self.version


def run(arguments: list[str], *, cwd: Path = ROOT, capture: bool = False) -> str:
    try:
        result = subprocess.run(
            arguments, cwd=cwd, check=True, text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        raise ReleaseError(f"命令失败：{' '.join(arguments)}") from exc
    except subprocess.CalledProcessError as exc:
        detail = "\n".join(
            part.strip() for part in (exc.stdout or "", exc.stderr or "") if part.strip()
        )
        suffix = f"\n{detail}" if detail else ""
        raise ReleaseError(
            f"命令失败（exit={exc.returncode}）：{' '.join(arguments)}{suffix}"
        ) from exc
    return result.stdout.strip() if capture else ""


def git(*arguments: str) -> str:
    return run(["git", *arguments], capture=True)


def current_version(explicit: str | None) -> str:
    value = explicit or os.environ.get("MCSP_VERSION")
    if not value:
        try:
            value = git("describe", "--tags", "--exact-match")
        except ReleaseError as exc:
            raise ReleaseError("需要 --version、MCSP_VERSION 或当前 commit 的 Git tag") from exc
    value = value.removeprefix("v")
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-_" for character in value):
        raise ReleaseError("版本只能包含字母、数字、点、减号和下划线")
    return value


def dirty() -> bool:
    return bool(git("status", "--porcelain"))


def sync_shared() -> None:
    for component in COMPONENTS:
        destination = ROOT / component / "src"
        for name in SHARED_FILES:
            shutil.copy2(ROOT / "lib" / name, destination / name)
        providers = destination / "providers"
        providers.mkdir(parents=True, exist_ok=True)
        for name in SHARED_PROVIDERS:
            shutil.copy2(ROOT / "lib/providers" / name, providers / name)
    optional = ROOT / "lib/modpack.py"
    if optional.is_file():
        for component in ("terminal", "manager"):
            shutil.copy2(optional, ROOT / component / "src/modpack.py")


def check_shared() -> None:
    for component in COMPONENTS:
        for name in SHARED_FILES:
            _same(ROOT / "lib" / name, ROOT / component / "src" / name)
        for name in SHARED_PROVIDERS:
            _same(ROOT / "lib/providers" / name, ROOT / component / "src/providers" / name)
    modpack = ROOT / "lib/modpack.py"
    if modpack.exists():
        for component in ("terminal", "manager"):
            _same(modpack, ROOT / component / "src/modpack.py")


def _same(source: Path, destination: Path) -> None:
    if not destination.is_file() or source.read_bytes() != destination.read_bytes():
        raise ReleaseError(f"共享源码未同步：{destination.relative_to(ROOT)}")


def check_repository(*, allow_dirty: bool) -> None:
    required = (
        "terminal/mcsp.sh", "terminal/config.yml.example", "terminal/.env.example",
        "manager/manager.sh", "manager/runtime/compose.yml",
        "monitor/monitor.sh", "monitor/compose.yml", "monitor/Dockerfile",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise ReleaseError("缺少发行文件：" + "、".join(missing))
    check_shared()
    if dirty() and not allow_dirty:
        raise ReleaseError("工作树不干净；提交变更或显式使用 --allow-dirty")
    run([sys.executable, "-m", "compileall", "-q", "terminal/src", "manager/src", "monitor/src"])
    for script in ("terminal/mcsp.sh", "manager/manager.sh", "monitor/monitor.sh"):
        if os.name != "nt":
            run(["bash", "-n", script])


def safe_component_files(component: str):
    root = ROOT / component
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            raise ReleaseError(f"发行内容不能包含符号链接：{path}")
        if not path.is_file():
            continue
        if relative in (Path(".env"), Path("config.yml")):
            continue
        if component == "terminal" and relative.parts[:1] in (("logs",), ("modpacks",)):
            if path.name != ".gitkeep":
                continue
        if component == "manager" and relative.parts[:1] == ("data",):
            continue
        if component == "manager" and relative.parts[:1] == ("modpacks",):
            if relative.parts[:2] != ("modpacks", "template"):
                continue
        if component == "monitor" and relative.parts[:1] == ("data",):
            if path.name != ".gitkeep":
                continue
        yield path, relative


def stage_component(component: str, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for source, relative in safe_component_files(component):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    for name in (("logs", "modpacks") if component == "terminal" else ("data",)):
        (destination / name).mkdir(exist_ok=True)


def tar_directory(source: Path, destination: Path, root_name: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source.rglob("*")):
            relative = Path(root_name) / path.relative_to(source)
            info = archive.gettarinfo(str(path), arcname=relative.as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            if path.is_file():
                with path.open("rb") as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    os.replace(temporary, destination)


def zip_directory(source: Path, destination: Path, root_name: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                name = (Path(root_name) / path.relative_to(source)).as_posix()
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if path.name in {"mcsp", "mcsp.exe"} else 0o644) << 16
                archive.writestr(info, path.read_bytes())
    os.replace(temporary, destination)


def build_component(context: Context, component: str) -> Path:
    stage = BUILD_ROOT / context.version / component
    stage_component(component, stage)
    output = context.output / f"mc-serverpack-{component}.tar.gz"
    tar_directory(stage, output, component)
    return output


def build_terminal(context: Context, target_platform: str) -> Path:
    suffix = "windows-x64.zip" if target_platform == "windows" else "linux-x64.tar.gz"
    stage = BUILD_ROOT / context.version / f"terminal-{target_platform}" / "MC-ServerPack"
    if stage.parent.exists():
        shutil.rmtree(stage.parent)
    stage.mkdir(parents=True)
    entry = stage.parent / "entry.py"
    entry.write_text("from terminal.src.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
    separator = ";" if os.name == "nt" else ":"
    executable_name = "mcsp.exe" if target_platform == "windows" else "mcsp"
    run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "mcsp", "--paths", str(ROOT),
        "--distpath", str(stage), "--workpath", str(stage.parent / "pyinstaller"),
        "--specpath", str(stage.parent),
        "--add-data", f"{ROOT / 'terminal/src/docker-install.sh'}{separator}terminal/src",
        "--collect-all", "tzdata",
        "--hidden-import", "terminal.src.providers.tencent",
        "--hidden-import", "terminal.src.providers.example",
        str(entry),
    ])
    built = stage / executable_name
    if not built.is_file():
        raise ReleaseError(f"PyInstaller 未生成 {built}")
    for name in ("config.yml.example", ".env.example", "readme.md"):
        shutil.copy2(ROOT / "terminal" / name, stage / name)
    for name in ("logs", "modpacks", "payloads"):
        (stage / name).mkdir()
    output = context.output / f"mc-serverpack-terminal-{suffix}"
    if target_platform == "windows":
        zip_directory(stage, output, "MC-ServerPack")
    else:
        tar_directory(stage, output, "MC-ServerPack")
    return output


def extract_archive(archive: Path, destination: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            _check_archive_names(source.namelist())
            source.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            _check_archive_names(member.name for member in source.getmembers())
            source.extractall(destination)


def _check_archive_names(names) -> None:
    for name in names:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ReleaseError(f"归档包含不安全路径：{name}")


def build_bundle(context: Context, target_platform: str) -> Path:
    terminal_suffix = "windows-x64.zip" if target_platform == "windows" else "linux-x64.tar.gz"
    terminal_archive = context.output / f"mc-serverpack-terminal-{terminal_suffix}"
    manager_archive = context.output / "mc-serverpack-manager.tar.gz"
    monitor_archive = context.output / "mc-serverpack-monitor.tar.gz"
    for path in (terminal_archive, manager_archive, monitor_archive):
        if not path.is_file():
            raise ReleaseError(f"构建 bundle 前缺少：{path.name}")
    stage = BUILD_ROOT / context.version / f"bundle-{target_platform}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    extract_archive(terminal_archive, stage)
    payloads = stage / "MC-ServerPack/payloads"
    payloads.mkdir(exist_ok=True)
    # Keep deployable trees beside the frozen executable.  A one-file
    # executable's modules are extracted to a transient PyInstaller directory,
    # so runtime deployment must never depend on that directory.
    for name, archive in (("manager", manager_archive), ("monitor", monitor_archive)):
        extracted = stage / f".{name}-payload"
        extracted.mkdir()
        extract_archive(archive, extracted)
        shutil.move(str(extracted / name), str(payloads / name))
        extracted.rmdir()
    suffix = "windows-x64.zip" if target_platform == "windows" else "linux-x64.tar.gz"
    output = context.output / f"mc-serverpack-bundle-{suffix}"
    if target_platform == "windows":
        zip_directory(stage / "MC-ServerPack", output, "MC-ServerPack")
    else:
        tar_directory(stage / "MC-ServerPack", output, "MC-ServerPack")
    return output


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_metadata(context: Context) -> None:
    context.output.mkdir(parents=True, exist_ok=True)
    artifacts = sorted(
        path for path in context.output.iterdir()
        if path.is_file() and path.name not in {"manifest.json", "checksums.sha256"}
    )
    entries = [{"name": path.name, "size": path.stat().st_size, "sha256": sha256(path)} for path in artifacts]
    manifest = {
        "format": 1, "version": context.version,
        "commit": git("rev-parse", "HEAD"), "dirty": dirty(),
        "artifacts": entries,
    }
    (context.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (context.output / "checksums.sha256").write_text(
        "".join(f"{item['sha256']}  {item['name']}\n" for item in entries), encoding="utf-8",
    )


def verify(context: Context) -> None:
    manifest_path = context.output / "manifest.json"
    if not manifest_path.is_file():
        raise ReleaseError("缺少 manifest.json；请先运行 checksums")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get("artifacts", []):
        path = context.output / item["name"]
        if not path.is_file() or sha256(path) != item["sha256"]:
            raise ReleaseError(f"发行校验失败：{path.name}")
    with tempfile.TemporaryDirectory(prefix="mcsp-release-") as raw:
        root = Path(raw)
        extracted: dict[str, Path] = {}
        for archive in context.output.iterdir():
            if archive.name.endswith((".tar.gz", ".zip")):
                target = root / archive.name
                target.mkdir()
                extract_archive(archive, target)
                extracted[archive.name] = target
                for path in target.rglob("*"):
                    if path.is_file() and path.name == ".env":
                        raise ReleaseError(f"发行包包含真实配置或密钥：{archive.name}:{path.name}")
        manager = extracted.get("mc-serverpack-manager.tar.gz")
        monitor = extracted.get("mc-serverpack-monitor.tar.gz")
        for platform_name in ("linux-x64.tar.gz", "windows-x64.zip"):
            bundle = extracted.get(f"mc-serverpack-bundle-{platform_name}")
            if not bundle:
                continue
            for name, standalone in (("manager", manager), ("monitor", monitor)):
                if standalone and _tree_hash(bundle / "MC-ServerPack/payloads" / name) != _tree_hash(standalone / name):
                    raise ReleaseError(f"捆绑包的 {name} payload 与独立包不一致")
        executable = (
            extracted.get("mc-serverpack-terminal-windows-x64.zip") / "MC-ServerPack/mcsp.exe"
            if os.name == "nt" and extracted.get("mc-serverpack-terminal-windows-x64.zip") else
            extracted.get("mc-serverpack-terminal-linux-x64.tar.gz") / "MC-ServerPack/mcsp"
            if os.name != "nt" and extracted.get("mc-serverpack-terminal-linux-x64.tar.gz") else None
        )
        if executable:
            run([str(executable), "--help"], cwd=executable.parent, capture=True)


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        raise ReleaseError(f"发行内容缺少目录：{root}")
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="MC-ServerPack 发行工具")
    result.add_argument("--version")
    result.add_argument("--allow-dirty", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("sync")
    commands.add_parser("check")
    build = commands.add_parser("build")
    build.add_argument("target", choices=("terminal", "manager", "monitor", "bundle", "all"))
    build.add_argument("--platform", choices=("linux", "windows"), default=("windows" if os.name == "nt" else "linux"))
    commands.add_parser("verify")
    commands.add_parser("checksums")
    return result


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    try:
        if args.command == "sync":
            sync_shared()
            return 0
        if args.command == "check":
            check_repository(allow_dirty=args.allow_dirty)
            return 0
        context = Context(current_version(args.version), args.allow_dirty)
        if args.command == "build":
            check_repository(allow_dirty=args.allow_dirty)
            context.output.mkdir(parents=True, exist_ok=True)
            if args.target in ("manager", "monitor"):
                build_component(context, args.target)
            elif args.target == "terminal":
                build_terminal(context, args.platform)
            elif args.target == "bundle":
                build_bundle(context, args.platform)
            else:
                build_component(context, "manager")
                build_component(context, "monitor")
                build_terminal(context, args.platform)
                build_bundle(context, args.platform)
            write_metadata(context)
        elif args.command == "checksums":
            write_metadata(context)
        elif args.command == "verify":
            verify(context)
        return 0
    except ReleaseError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
