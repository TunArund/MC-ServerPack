#!/usr/bin/env python3
"""MC-ServerPack reproducible release builder."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zipapp
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RELEASE_ROOT = ROOT / "dev/release"
BUILD_ROOT = RELEASE_ROOT / "build"
DIST_ROOT = RELEASE_ROOT / "dist"
SHARED_FILES = (
    "config_sdk.py",
    "docker_sdk.py",
    "manager_sdk.py",
    "observability_sdk.py",
    "power_sdk.py",
)
SHARED_MODPACK_FILES = ("modpack_sdk.py",)
SHARED_TERMINAL_FILES = ("docker-install.sh",)
SHARED_PROVIDERS = ("example.py", "tencent.py")
COMPONENTS = ("terminal", "manager", "monitor")
EXCLUDED_NAMES = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".git", ".venv",
    "build", "dist",
}
SECRET_NAMES = {".env", "config.yml"}
RUNTIME_FORMAT = 1


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
            capture_output=capture, encoding="utf-8", errors="replace",
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
    for component in ("terminal", "manager"):
        for name in SHARED_MODPACK_FILES:
            shutil.copy2(ROOT / "lib" / name, ROOT / component / "src" / name)
    for name in SHARED_TERMINAL_FILES:
        shutil.copy2(ROOT / "lib" / name, ROOT / "terminal/src" / name)


def check_shared() -> None:
    for component in COMPONENTS:
        for name in SHARED_FILES:
            _same(ROOT / "lib" / name, ROOT / component / "src" / name)
        for name in SHARED_PROVIDERS:
            _same(ROOT / "lib/providers" / name, ROOT / component / "src/providers" / name)
    for component in ("terminal", "manager"):
        for name in SHARED_MODPACK_FILES:
            _same(ROOT / "lib" / name, ROOT / component / "src" / name)
    for name in SHARED_TERMINAL_FILES:
        _same(ROOT / "lib" / name, ROOT / "terminal/src" / name)


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
    output = context.output / f"mcsp-{component}.tar.gz"
    tar_directory(stage, output, component)
    return output


def _build_application_archive(destination: Path) -> None:
    source = destination.parent / ".mcsp-app"
    if source.exists():
        shutil.rmtree(source)
    (source / "terminal/src").mkdir(parents=True)
    (source / "terminal/__init__.py").write_text("", encoding="utf-8")
    (source / "terminal/src/__init__.py").write_text("", encoding="utf-8")
    for path in sorted((ROOT / "terminal/src").rglob("*")):
        relative = path.relative_to(ROOT / "terminal/src")
        if path.is_file() and "__pycache__" not in relative.parts and path.suffix == ".py":
            target = source / "terminal/src" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    (source / "__main__.py").write_text(
        "from terminal.src.cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(source, destination, compressed=True)
    shutil.rmtree(source)


def stage_data(context: Context, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    _build_application_archive(destination / "mcsp.pyz")
    for name in ("config.yml.example", ".env.example", "readme.md"):
        shutil.copy2(ROOT / "terminal" / name, destination / name)
    shutil.copy2(ROOT / "lib/docker-install.sh", destination / "docker-install.sh")
    shutil.copytree(ROOT / "terminal/modpacks/template", destination / "modpacks/template")
    for component in ("manager", "monitor"):
        stage_component(component, destination / component)
    (destination / "version.json").write_text(
        json.dumps({
            "format": 1,
            "version": context.version,
            "runtime-format": RUNTIME_FORMAT,
            "commit": git("rev-parse", "HEAD"),
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_data(context: Context) -> Path:
    data = BUILD_ROOT / context.version / "data" / "data"
    stage_data(context, data)
    output = context.output / "mcsp-data.zip"
    zip_directory(data, output, "data")
    return output


def _extract_data(context: Context, destination: Path) -> None:
    archive = context.output / "mcsp-data.zip"
    if not archive.is_file():
        build_data(context)
    extract_archive(archive, destination)


def application_imports() -> tuple[str, ...]:
    """Return absolute imports needed by the external Terminal application."""
    names: set[str] = set()
    for path in (ROOT / "terminal/src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
    result = []
    for name in sorted(names):
        if name.split(".", 1)[0] in {"terminal", "manager", "monitor", "lib"}:
            continue
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, AttributeError):
            available = False
        if available:
            result.append(name)
    return tuple(result)


def build_platform(context: Context, target_platform: str) -> Path:
    expected_os = "windows" if os.name == "nt" else "linux"
    if target_platform != expected_os:
        raise ReleaseError(f"{target_platform} 运行库必须在对应原生平台构建")
    root = BUILD_ROOT / context.version / f"platform-{target_platform}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    entry = root / "launcher.py"
    entry.write_text(
        "from pathlib import Path\n"
        "import importlib\n"
        "import sys\n\n"
        "archive = Path(sys.executable).resolve().parent / 'data/mcsp.pyz'\n"
        "if not archive.is_file():\n"
        "    raise SystemExit(f'缺少程序数据：{archive}')\n"
        "sys.path.insert(0, str(archive))\n"
        "main = importlib.import_module('terminal.src.cli').main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    dist = root / "pyinstaller-dist"
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--contents-directory", "lib", "--name", "mcsp",
        "--distpath", str(dist), "--workpath", str(root / "pyinstaller"),
        "--specpath", str(root),
        "--collect-all", "tzdata",
    ]
    for name in application_imports():
        command.extend(("--hidden-import", name))
    command.append(str(entry))
    run(command)
    built = dist / "mcsp"
    stage = root / "MC-ServerPack"
    shutil.copytree(built, stage)
    executable = stage / ("mcsp.exe" if target_platform == "windows" else "mcsp")
    if not executable.is_file() or not (stage / "lib").is_dir():
        raise ReleaseError("PyInstaller 未生成模块化启动器与 lib 运行库")
    _extract_data(context, stage)
    for name in ("logs", "modpacks", "state"):
        (stage / name).mkdir()
    suffix = "windows-x64.zip" if target_platform == "windows" else "linux-x64.tar.gz"
    output = context.output / f"mcsp-{suffix}"
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
        if path.is_file() and path.name != "release.json"
    )
    entries = {
        path.name: {"size": path.stat().st_size, "sha256": sha256(path)}
        for path in artifacts
    }
    manifest = {
        "format": 1, "version": context.version, "runtime-format": RUNTIME_FORMAT,
        "commit": git("rev-parse", "HEAD"), "dirty": dirty(),
        "files": entries,
    }
    (context.output / "release.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def verify(context: Context) -> None:
    manifest_path = context.output / "release.json"
    if not manifest_path.is_file():
        raise ReleaseError("缺少 release.json；请先运行 metadata")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("runtime-format") != RUNTIME_FORMAT:
        raise ReleaseError("release.json runtime-format 不兼容")
    for name, item in manifest.get("files", {}).items():
        path = context.output / name
        if (not path.is_file() or path.stat().st_size != item.get("size")
                or sha256(path) != item.get("sha256")):
            raise ReleaseError(f"发行校验失败：{name}")
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
        manager = extracted.get("mcsp-manager.tar.gz")
        monitor = extracted.get("mcsp-monitor.tar.gz")
        data = extracted.get("mcsp-data.zip")
        if data:
            version = json.loads((data / "data/version.json").read_text(encoding="utf-8"))
            if version.get("version") != context.version or version.get("runtime-format") != RUNTIME_FORMAT:
                raise ReleaseError("mcsp-data.zip 版本信息不正确")
            for name, standalone in (("manager", manager), ("monitor", monitor)):
                if standalone and _tree_hash(data / "data" / name) != _tree_hash(standalone / name):
                    raise ReleaseError(f"data/{name} 与独立包不一致")
        for platform_name in ("linux-x64.tar.gz", "windows-x64.zip"):
            package = extracted.get(f"mcsp-{platform_name}")
            if not package:
                continue
            installation = package / "MC-ServerPack"
            if not (installation / "lib").is_dir() or not (installation / "data/mcsp.pyz").is_file():
                raise ReleaseError(f"完整包结构不正确：mcsp-{platform_name}")
            if data and _tree_hash(installation / "data") != _tree_hash(data / "data"):
                raise ReleaseError(f"完整包 data 与 mcsp-data.zip 不一致：{platform_name}")
        executable = (
            extracted.get("mcsp-windows-x64.zip") / "MC-ServerPack/mcsp.exe"
            if os.name == "nt" and extracted.get("mcsp-windows-x64.zip") else
            extracted.get("mcsp-linux-x64.tar.gz") / "MC-ServerPack/mcsp"
            if os.name != "nt" and extracted.get("mcsp-linux-x64.tar.gz") else None
        )
        if executable:
            try:
                smoke_commands = (
                    ("--help",),
                    ("config", "init"),
                    ("config", "check"),
                    ("modpack", "create", "release-test"),
                    ("modpack", "check", "release-test"),
                    ("modpack", "export", "release-test"),
                    ("modpack", "remove", "release-test", "--force"),
                    ("modpack", "import", "release-test"),
                )
                for arguments in smoke_commands:
                    run([str(executable), *arguments], cwd=executable.parent, capture=True)
            except ReleaseError as exc:
                log = executable.parent / "logs/terminal.log"
                detail = log.read_text(encoding="utf-8", errors="replace")[-8000:] if log.is_file() else ""
                suffix = f"\nTerminal log:\n{detail}" if detail else ""
                raise ReleaseError(f"{exc}{suffix}") from exc


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
    build.add_argument("target", choices=("data", "platform", "manager", "monitor", "all"))
    build.add_argument("--platform", choices=("linux", "windows"), default=("windows" if os.name == "nt" else "linux"))
    commands.add_parser("verify")
    commands.add_parser("metadata")
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
            elif args.target == "data":
                build_data(context)
            elif args.target == "platform":
                build_platform(context, args.platform)
            else:
                build_component(context, "manager")
                build_component(context, "monitor")
                build_data(context)
                build_platform(context, args.platform)
            write_metadata(context)
        elif args.command == "metadata":
            write_metadata(context)
        elif args.command == "verify":
            verify(context)
        return 0
    except ReleaseError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
