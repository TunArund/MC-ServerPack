"""Manager 标准归档与安全导入。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import uuid
import time
from datetime import datetime
from pathlib import Path

from .config import load_pack, validate_pack_id
from .modpack_sdk import (
    ModpackError, archive_members, export_pack, file_sha256, verify_extracted,
)
from .modpacks import ModpackManager

CHUNK_SIZE = 4 * 1024 * 1024
TRANSFER_QUOTA = 256 * 1024**3
TRANSFER_TTL = 24 * 3600


def import_pack(manager: ModpackManager, pack_id: str, archive_path: Path, *, force: bool) -> Path:
    validate_pack_id(pack_id)
    target = manager.packs_dir / pack_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        raise ModpackError(f"整合包已存在：{pack_id}；确认覆盖请使用 force=true")
    if target.is_symlink() or any((target / name).is_symlink() for name in ("src", "data", "backups", "config.yml")):
        raise ModpackError(f"整合包目标不是安全目录：{pack_id}")
    if target.is_dir() and manager.docker.running(manager.spec(pack_id)):
        raise ModpackError("导入覆盖前必须先停止整合包")
    # 与 create 相同：暂存目录必须在 modpacks 同文件系统，保证最终 rename 原子生效。
    staging_root = manager.packs_dir / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    manager.set_maintenance(pack_id, True)
    try:
        with tempfile.TemporaryDirectory(prefix=f"import-{pack_id}-", dir=staging_root) as raw:
            parent = Path(raw); staging = parent / pack_id; staging.mkdir()
            try:
                with tarfile.open(archive_path, "r:gz") as archive:
                    archive.extractall(staging, members=archive_members(archive))
            except (OSError, tarfile.TarError) as exc:
                raise ModpackError(f"整合包归档处理失败：{exc}") from exc
            manifest = verify_extracted(staging, pack_id)
            load_pack(pack_id, parent)
            if manifest["mode"] == "source-only" and target.is_dir():
                for name in ("data", "backups"):
                    existing = target / name
                    if existing.is_dir(): shutil.copytree(existing, staging / name)
            (staging / "data").mkdir(exist_ok=True); (staging / "backups").mkdir(exist_ok=True)
            (staging / "manifest.json").unlink()
            previous = target.parent / f".{pack_id}.previous"
            shutil.rmtree(previous, ignore_errors=True)
            try:
                if target.exists(): target.rename(previous)
                staging.rename(target)
                manager.configure(pack_id)
                manager.set_enabled(pack_id, False)
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                if previous.exists(): previous.rename(target)
                raise
            shutil.rmtree(previous, ignore_errors=True)
        return target
    finally:
        manager.set_maintenance(pack_id, False)


class TransferStore:
    """将可恢复分块和元数据限制在 data/transfers。"""

    def __init__(self, root: Path) -> None:
        self.root = root / "data/transfers"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cleanup()

    def cleanup(self) -> None:
        now = time.time()
        for directory in self.root.iterdir():
            if directory.is_dir() and now - directory.stat().st_mtime > TRANSFER_TTL:
                shutil.rmtree(directory, ignore_errors=True)

    def _reserve(self, size: int) -> None:
        used = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        if used + size > TRANSFER_QUOTA:
            raise ModpackError("传输暂存区超过 256 GiB 配额")

    def create_upload(self, pack_id: str, size: int, sha256: str, *, force: bool) -> dict:
        validate_pack_id(pack_id)
        if type(size) is not int or size <= 0 or size > 128 * 1024**3:
            raise ModpackError("归档大小无效或超过 128 GiB 限制")
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ModpackError("归档 SHA-256 无效")
        self._reserve(size)
        transfer_id = uuid.uuid4().hex
        directory = self.root / transfer_id; directory.mkdir(mode=0o700)
        metadata = {"direction": "upload", "pack_id": pack_id, "size": size,
                    "sha256": sha256, "chunk_size": CHUNK_SIZE, "force": force,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write(directory, metadata)
        return {"transfer_id": transfer_id, **metadata}

    def create_download(self, pack_id: str, archive: Path) -> dict:
        transfer_id = uuid.uuid4().hex
        self._reserve(archive.stat().st_size)
        directory = self.root / transfer_id; directory.mkdir(mode=0o700)
        target = directory / "archive.tgz"; shutil.move(str(archive), target)
        metadata = {"direction": "download", "pack_id": pack_id,
                    "size": target.stat().st_size, "sha256": file_sha256(target),
                    "chunk_size": CHUNK_SIZE,
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        self._write(directory, metadata)
        return {"transfer_id": transfer_id, **metadata}

    def _directory(self, transfer_id: str) -> Path:
        if len(transfer_id) != 32 or any(c not in "0123456789abcdef" for c in transfer_id):
            raise ModpackError("传输 ID 无效")
        directory = self.root / transfer_id
        if not directory.is_dir(): raise ModpackError(f"传输不存在：{transfer_id}")
        return directory

    @staticmethod
    def _write(directory: Path, value: dict) -> None:
        (directory / "metadata.json").write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")

    def metadata(self, transfer_id: str) -> dict:
        try: value = json.loads((self._directory(transfer_id) / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc: raise ModpackError("传输元数据损坏") from exc
        if not isinstance(value, dict): raise ModpackError("传输元数据损坏")
        return value

    def put_chunk(self, transfer_id: str, index: int, content: bytes, digest: str) -> dict:
        metadata = self.metadata(transfer_id)
        if metadata.get("direction") != "upload" or index < 0: raise ModpackError("上传分块参数无效")
        expected_size = min(CHUNK_SIZE, metadata["size"] - index * CHUNK_SIZE)
        if expected_size <= 0 or len(content) != expected_size: raise ModpackError("上传分块大小无效")
        actual = hashlib.sha256(content).hexdigest()
        if actual != digest: raise ModpackError("上传分块哈希不一致")
        path = self._directory(transfer_id) / f"chunk-{index:08d}"
        temporary = path.with_suffix(".tmp"); temporary.write_bytes(content); os.replace(temporary, path)
        return {"index": index, "size": len(content), "sha256": actual}

    def assemble(self, transfer_id: str) -> tuple[Path, dict]:
        directory = self._directory(transfer_id); metadata = self.metadata(transfer_id)
        target = directory / "archive.tgz"; temporary = directory / "archive.tmp"
        count = (metadata["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE
        with temporary.open("wb") as output:
            for index in range(count):
                path = directory / f"chunk-{index:08d}"
                if not path.is_file(): raise ModpackError(f"缺少上传分块：{index}")
                with path.open("rb") as source: shutil.copyfileobj(source, output)
        if temporary.stat().st_size != metadata["size"] or file_sha256(temporary) != metadata["sha256"]:
            temporary.unlink(missing_ok=True); raise ModpackError("完整归档大小或哈希不一致")
        os.replace(temporary, target)
        return target, metadata

    def get_chunk(self, transfer_id: str, index: int) -> tuple[bytes, dict]:
        directory = self._directory(transfer_id); metadata = self.metadata(transfer_id)
        if metadata.get("direction") != "download" or index < 0: raise ModpackError("下载分块参数无效")
        with (directory / "archive.tgz").open("rb") as source:
            source.seek(index * CHUNK_SIZE); content = source.read(CHUNK_SIZE)
        if not content: raise ModpackError(f"下载分块不存在：{index}")
        return content, {"index": index, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
