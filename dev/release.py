"""Build and optionally publish a native release to Gitee."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://gitee.com/api/v5"
VERSION_RE = re.compile(r"^v\d+\.\d+$")
REMOTE_RE = re.compile(r"(?:https://gitee\.com/|git@gitee\.com:)([^/]+)/([^/]+?)(?:\.git)?$")
EXCLUDED = {"dev", ".github", ".gitee", ".idea", ".vscode"}
UPDATE_MARKER = "MC_SERVERPACK_RELEASE_UPDATED"


class ReleaseError(RuntimeError):
    pass


def git(*args: str, capture: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=capture,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise ReleaseError(f"git {' '.join(args)} 失败：{detail}")
    return result.stdout.strip() if capture else ""


def repository(remote: str) -> tuple[str, str]:
    url = git("remote", "get-url", remote)
    match = REMOTE_RE.search(url)
    if not match:
        raise ReleaseError(f"{remote} 不是可识别的 Gitee 远端：{url}")
    return match.group(1), match.group(2)


def precheck(version: str, remote: str) -> tuple[str, str]:
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError("版本必须类似 v0.1")
    if git("status", "--porcelain"):
        raise ReleaseError("工作区不干净；请先提交或清理修改")
    try:
        tagged = git("rev-list", "-n", "1", version)
    except ReleaseError as exc:
        raise ReleaseError(f"本地不存在标签 {version}；请先创建标签") from exc
    if tagged != git("rev-parse", "HEAD"):
        raise ReleaseError(f"标签 {version} 不指向当前 HEAD")
    return repository(remote)


def update_checkout(version: str, remote: str) -> None:
    if os.environ.get(UPDATE_MARKER) == "1":
        return
    if git("status", "--porcelain"):
        raise ReleaseError("工作区不干净，不能自动更新；请先提交或清理修改")
    try:
        branch = git("symbolic-ref", "--short", "HEAD")
    except ReleaseError as exc:
        raise ReleaseError("自动更新需要处于分支上，不能使用 detached HEAD") from exc
    git("fetch", remote, branch)
    git("merge", "--ff-only", f"{remote}/{branch}")
    git("fetch", remote, f"+refs/tags/{version}:refs/tags/{version}")
    os.environ[UPDATE_MARKER] = "1"
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])


def check_remote_tag(version: str, remote: str) -> None:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--tags", remote, f"refs/tags/{version}"],
        cwd=ROOT, text=True, capture_output=True,
    )
    if result.returncode:
        raise ReleaseError(f"远端 {remote} 没有标签 {version}；请先 push 标签")
    remote_revision = result.stdout.split()[0]
    local_revision = git("rev-parse", version)
    if remote_revision != local_revision:
        raise ReleaseError(
            f"本地标签 {version} 与远端不一致；请先强制更新本地标签："
            f"git fetch {remote} +refs/tags/{version}:refs/tags/{version}"
        )


def platform_info() -> tuple[str, str]:
    system = platform.system().lower()
    systems = {"windows": "windows", "linux": "linux"}
    machines = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
    if system not in systems or platform.machine().lower() not in machines:
        raise ReleaseError(f"暂不支持的构建平台：{platform.system()} {platform.machine()}")
    return f"{systems[system]}-{machines[platform.machine().lower()]}", "mc.exe" if system == "windows" else "mc"


def build_executable(work: Path, executable_name: str) -> Path:
    dist, build = work / "dist", work / "build"
    command = [
        sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", "--onefile",
        "--name", "mc", "--paths", str(ROOT), "--distpath", str(dist),
        "--workpath", str(build), "--specpath", str(work),
        str(ROOT / "lib/executable.py"),
    ]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise ReleaseError("PyInstaller 构建失败")
    executable = dist / executable_name
    if not executable.is_file():
        raise ReleaseError(f"PyInstaller 未生成 {executable_name}")
    return executable


def export_source(version: str, destination: Path) -> None:
    result = subprocess.run(
        ["git", "archive", "--format=tar", version], cwd=ROOT, capture_output=True,
    )
    if result.returncode:
        raise ReleaseError(result.stderr.decode(errors="replace").strip() or "git archive 失败")
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        root = destination.resolve()
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            if target != root and root not in target.parents:
                raise ReleaseError(f"源码归档包含不安全路径：{member.name}")
            if not (member.isfile() or member.isdir()):
                raise ReleaseError(f"源码归档包含链接或特殊文件：{member.name}")
        # Members were constrained to regular files/directories and checked to stay under destination.
        archive.extractall(destination)
    for name in EXCLUDED:
        target = destination / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def make_zip(
    version: str, platform_name: str, executable_name: str,
    executable: Path, output_dir: Path,
) -> tuple[Path, Path]:
    label = version.removeprefix("v")
    package_name = f"MC-ServerPack-{label}-{platform_name}"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{package_name}.zip"
    with tempfile.TemporaryDirectory(prefix="mc-release-") as raw:
        package = Path(raw) / package_name
        package.mkdir()
        export_source(version, package)
        # The terminal package is embedded in the native executable. Keep
        # manager/lib because it is deployed separately to mc-server.
        shutil.rmtree(package / "lib")
        (package / ".gitignore").unlink(missing_ok=True)
        if platform_name.startswith("windows-"):
            (package / "mc.sh").unlink()
        target = package / executable_name
        shutil.copy2(executable, target)
        if executable_name == "mc":
            target.chmod(0o755)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(package.parent).as_posix())
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return output, checksum


def extract_package(archive_path: Path, destination: Path) -> Path:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    package_name = archive_path.stem
    target = destination / package_name
    with tempfile.TemporaryDirectory(prefix="mc-release-extract-", dir=destination) as raw:
        temporary = Path(raw)
        with zipfile.ZipFile(archive_path) as archive:
            root = temporary.resolve()
            for member in archive.infolist():
                path = (temporary / member.filename).resolve()
                if path != root and root not in path.parents:
                    raise ReleaseError(f"发行包包含不安全路径：{member.filename}")
            archive.extractall(temporary)
        extracted = temporary / package_name
        if not extracted.is_dir():
            raise ReleaseError(f"发行包缺少顶层目录：{package_name}")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), target)
    return target


def release_notes(version: str) -> str:
    tags = git("tag", "--sort=-version:refname").splitlines()
    previous = next((tag for tag in tags if tag != version and VERSION_RE.fullmatch(tag)), None)
    revision = f"{previous}..{version}" if previous else version
    notes = git("log", revision, "--pretty=format:- %s")
    return notes or version


def local_secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return ""
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("\"'")
    return ""


def request_json(url: str, *, fields: dict[str, str], token: str) -> dict:
    payload = urllib.parse.urlencode({**fields, "access_token": token}).encode()
    request = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ReleaseError(f"Gitee API 返回 {exc.code}：{detail}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"无法连接 Gitee API：{exc.reason}") from exc


def find_release(owner: str, repo: str, version: str, token: str) -> dict | None:
    tag = urllib.parse.quote(version, safe="")
    query = urllib.parse.urlencode({"access_token": token})
    request = urllib.request.Request(
        f"{API}/repos/{owner}/{repo}/releases/tags/{tag}?{query}", method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        detail = exc.read().decode(errors="replace")
        raise ReleaseError(f"查询 Gitee Release 失败（{exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"无法连接 Gitee API：{exc.reason}") from exc


def upload(url: str, path: Path, token: str) -> dict:
    boundary = f"----mc-serverpack-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in (("access_token", token),):
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    chunks.extend((path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()))
    request = urllib.request.Request(url, data=b"".join(chunks), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ReleaseError(f"上传 {path.name} 失败（{exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise ReleaseError(f"上传 {path.name} 时无法连接 Gitee API：{exc.reason}") from exc


def publish(owner: str, repo: str, version: str, notes: str, files: tuple[Path, ...]) -> None:
    token = local_secret("GITEE_TOKEN")
    if not token:
        raise ReleaseError("发布需要在进程环境或根 .env 中设置 GITEE_TOKEN")
    release = find_release(owner, repo, version, token)
    if release:
        print(f"复用已有 Gitee Release：{version}")
    else:
        release = request_json(
            f"{API}/repos/{owner}/{repo}/releases", token=token,
            fields={
                "tag_name": version, "name": version, "body": notes,
                "target_commitish": version, "prerelease": "false",
            },
        )
    release_id = release.get("id")
    if not release_id:
        raise ReleaseError(f"Gitee 未返回 Release ID：{release}")
    url = f"{API}/repos/{owner}/{repo}/releases/{release_id}/attach_files"
    for path in files:
        result = upload(url, path, token)
        print(f"已上传：{result.get('browser_download_url', path.name)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="构建并发布当前平台的 Gitee 发行包")
    parser.add_argument("version", help="版本标签，例如 v0.1")
    parser.add_argument("--publish", action="store_true", help="创建 Gitee Release 并上传附件")
    parser.add_argument("--update", action="store_true", help="先快进当前分支并同步远端版本标签")
    parser.add_argument(
        "--extract", nargs="?", const=ROOT.parent, type=Path, metavar="DIR",
        help="构建后覆盖解压到 DIR；省略 DIR 时使用仓库的上级目录",
    )
    parser.add_argument("--remote", default="origin", help="Gitee Git 远端名，默认 origin")
    parser.add_argument("--output", type=Path, default=ROOT / "dev/dist", help="发行包输出目录")
    args = parser.parse_args()
    if args.update:
        update_checkout(args.version, args.remote)
    owner, repo = precheck(args.version, args.remote)
    check_remote_tag(args.version, args.remote)
    print(f"构建提交：{git('rev-parse', '--short', args.version)} {args.version}")
    platform_name, executable_name = platform_info()
    with tempfile.TemporaryDirectory(prefix="mc-release-build-") as raw:
        executable = build_executable(Path(raw), executable_name)
        package, checksum = make_zip(
            args.version, platform_name, executable_name, executable, args.output.resolve(),
        )
    print(f"已生成：{package}")
    print(f"已生成：{checksum}")
    if args.extract is not None:
        print(f"已解压：{extract_package(package, args.extract)}")
    if args.publish:
        publish(owner, repo, args.version, release_notes(args.version), (package, checksum))
        print(f"Gitee Release 发布完成：{owner}/{repo} {args.version}")
    else:
        print("未上传；确认产物后追加 --publish 发布到 Gitee。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ReleaseError, UnicodeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(2)
