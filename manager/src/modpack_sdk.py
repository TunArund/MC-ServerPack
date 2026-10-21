"""共享整合包工作区、清单与安全归档 SDK。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


FORMAT = 1
PACK_ID_RE = re.compile(r"[A-Za-z0-9-]+")
ALLOWED_ROOTS = {"config.yml", "src", "data", "backups", "manifest.json"}


class ModpackError(RuntimeError):
    pass


def validate_pack_id(pack_id: str) -> str:
    if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id) or pack_id == "template":
        raise ModpackError(f"无效整合包 ID：{pack_id}")
    return pack_id


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pack_files(source: Path, roots: tuple[str, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    for root_name in roots:
        root = source / root_name
        if root.is_symlink():
            raise ModpackError(f"整合包不能包含符号链接：{root_name}")
        paths = [root] if root.is_file() else sorted(root.rglob("*")) if root.is_dir() else []
        for path in paths:
            if path.is_symlink():
                raise ModpackError(f"整合包不能包含符号链接：{path.relative_to(source)}")
            if path.is_file():
                result[path.relative_to(source).as_posix()] = f"sha256:{file_sha256(path)}"
    return result


def inspect_pack(pack_id: str, packs_dir: Path) -> dict[str, object]:
    validate_pack_id(pack_id)
    return _inspect_directory(pack_id, packs_dir / pack_id)


def _inspect_directory(pack_id: str, directory: Path) -> dict[str, object]:
    if not (directory / "src").is_dir():
        raise ModpackError(f"{pack_id} 缺少 src/ 目录")
    if yaml is None:
        raise ModpackError("缺少 PyYAML")
    try:
        document = yaml.safe_load((directory / "config.yml").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModpackError(f"{pack_id} 缺少 config.yml") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ModpackError(f"{pack_id} 配置读取失败：{exc}") from exc
    if not isinstance(document, dict):
        raise ModpackError(f"{pack_id}/config.yml 必须是对象")
    name = document.get("name", pack_id)
    version = document.get("version", "unknown")
    java = document.get("java")
    memory = document.get("mem-limit")
    port = document.get("port", 25565)
    if not isinstance(name, str) or not name.strip():
        raise ModpackError("name 必须是非空字符串")
    if not isinstance(version, (str, int, float)) or isinstance(version, bool):
        raise ModpackError("version 必须是字符串或数字")
    if type(java) is not int or java not in (8, 17, 21):
        raise ModpackError("java 只能是 8、17 或 21")
    if not isinstance(memory, str) or not re.fullmatch(r"[1-9]\d*[kKmMgGtT](?:[bB])?", memory):
        raise ModpackError("mem-limit 必须是类似 8g 的内存大小")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ModpackError("port 必须是 1 到 65535 的整数")
    return {
        "id": pack_id, "name": name.strip(), "version": str(version),
        "java": java, "memory": memory.lower().removesuffix("b"), "port": port,
        "files": len(pack_files(directory, ("config.yml", "src"))),
    }


def export_pack(
    pack_id: str, packs_dir: Path, *, destination: Path,
    mode: str = "full", include_backups: bool = False,
) -> Path:
    details = inspect_pack(pack_id, packs_dir)
    if mode not in ("full", "source-only"):
        raise ModpackError("导出模式只能是 full 或 source-only")
    source = packs_dir / pack_id
    roots = ["config.yml", "src"]
    if mode == "full":
        roots.append("data")
    if include_backups:
        roots.append("backups")
    manifest = {
        "format": FORMAT, "pack_id": pack_id, "name": details["name"],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode, "include_backups": include_backups,
        "files": pack_files(source, tuple(roots)),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="mcsp-manifest-") as raw:
            manifest_path = Path(raw) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            with tarfile.open(temporary, "w:gz") as archive:
                archive.add(manifest_path, arcname="manifest.json")
                for name in roots:
                    path = source / name
                    if path.exists():
                        archive.add(path, arcname=name, recursive=True)
        os.replace(temporary, destination)
    except (OSError, tarfile.TarError) as exc:
        temporary.unlink(missing_ok=True)
        raise ModpackError(f"整合包导出失败：{exc}") from exc
    return destination


def archive_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    names: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        normalized = path.as_posix().rstrip("/")
        if (path.is_absolute() or not path.parts or path.parts[0] not in ALLOWED_ROOTS
                or ".." in path.parts or normalized in names
                or normalized != member.name.rstrip("/") or "\\" in member.name
                or not (member.isfile() or member.isdir())):
            raise ModpackError(f"整合包归档包含不安全路径：{member.name}")
        names.add(normalized)
    return members


def verify_extracted(root: Path, pack_id: str) -> dict:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        raise ModpackError(f"整合包 manifest.json 无效：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ModpackError("不支持的整合包归档格式")
    if manifest.get("pack_id") != pack_id or manifest.get("mode") not in ("full", "source-only"):
        raise ModpackError("整合包清单身份或模式无效")
    files = manifest.get("files")
    roots = tuple(name for name in ("config.yml", "src", "data", "backups") if (root / name).exists())
    if not isinstance(files, dict) or pack_files(root, roots) != files:
        raise ModpackError("整合包文件与 manifest.json 哈希不一致")
    return manifest


class ModpackWorkspace:
    def __init__(self, packs_dir: Path, template: Path | None = None) -> None:
        self.packs_dir = packs_dir
        self.template = template or packs_dir / "template"

    def create(self, pack_id: str, *, force: bool = False) -> Path:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        exists = target.exists() or target.is_symlink()
        if exists and not force:
            raise ModpackError(f"整合包已存在：{pack_id}")
        if not self.template.is_dir():
            raise ModpackError(f"缺少整合包模板：{self.template}")
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        staging_root = self.packs_dir / ".staging"
        staging_root.mkdir(exist_ok=True)
        staging = staging_root / f"{pack_id}.create-{uuid.uuid4().hex}"
        backup: Path | None = None
        try:
            shutil.copytree(self.template, staging, ignore=shutil.ignore_patterns("compose.yml", "__pycache__"))
            config = staging / "config.yml"
            content = re.sub(r"(?m)^name:\s*.*$", f"name: {pack_id}", config.read_text(encoding="utf-8"), count=1)
            config.write_text(content, encoding="utf-8")
            (staging / "src").mkdir(exist_ok=True)
            if exists:
                backup = staging_root / f"{pack_id}.replace-{uuid.uuid4().hex}"
                target.replace(backup)
            try:
                staging.replace(target)
            except Exception:
                if backup is not None:
                    backup.replace(target)
                raise
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if backup is not None:
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink(missing_ok=True)
        return target

    def remove(self, pack_id: str) -> None:
        validate_pack_id(pack_id)
        target = self.packs_dir / pack_id
        if not target.is_dir() or target.is_symlink():
            raise ModpackError(f"整合包不存在：{pack_id}")
        shutil.rmtree(target)

    def list(self) -> list[dict[str, object]]:
        if not self.packs_dir.is_dir():
            return []
        return [inspect_pack(path.name, self.packs_dir) for path in sorted(self.packs_dir.iterdir())
                if path.is_dir() and path.name != "template" and not path.name.startswith(".")]

    def check(self, pack_id: str) -> dict[str, object]:
        return inspect_pack(pack_id, self.packs_dir)

    def export(self, pack_id: str) -> Path:
        return export_pack(
            pack_id, self.packs_dir, mode="source-only",
            destination=self.packs_dir / f"{pack_id}.tgz",
        )

    def import_archive(self, pack_id: str) -> Path:
        validate_pack_id(pack_id)
        source = self.packs_dir / f"{pack_id}.tgz"
        if not source.is_file():
            raise ModpackError(f"整合包归档不存在：{source}")
        target = self.packs_dir / pack_id
        if target.exists() or target.is_symlink():
            raise ModpackError(f"整合包已存在：{pack_id}")
        self.packs_dir.mkdir(parents=True, exist_ok=True)
        staging = self.packs_dir / f".{pack_id}.import-{uuid.uuid4().hex}"
        staging.mkdir()
        try:
            with tarfile.open(source, "r:gz") as archive:
                archive.extractall(staging, members=archive_members(archive))
            verify_extracted(staging, pack_id)
            _inspect_directory(pack_id, staging)
            (staging / "manifest.json").unlink()
            staging.replace(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return target
