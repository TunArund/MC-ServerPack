"""
远程连接、Python SSH/SFTP 与增量同步

"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from collections import deque
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath

from .output import renderer


LOGGER = logging.getLogger(__name__)


class CommandError(RuntimeError):
    def __init__(self, message: str, *, reported: bool = False) -> None:
        super().__init__(message)
        self.reported = reported


HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
STEP_RE = re.compile(r"^\[[^\]]+ \d+/\d+\]\s+.+")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def fit_terminal(value: str, max_cells: int) -> str:
    """按终端显示宽度截断，避免中文导致换行后破坏原地刷新。"""
    clean = ANSI_RE.sub("", value).replace("\t", "    ")
    rendered: list[str] = []
    cells = 0
    for character in clean:
        if unicodedata.category(character) == "Cc":
            continue
        width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in ("W", "F") else 1
        )
        if cells + width > max_cells:
            break
        rendered.append(character)
        cells += width
    return "".join(rendered)


def run(
    arguments: Sequence[str], *, input_data: bytes | None = None,
    capture: bool = False, check: bool = True,
) -> subprocess.CompletedProcess:
    command = [str(item) for item in arguments]
    try:
        return subprocess.run(
            command, input=input_data, capture_output=capture, check=check,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"找不到命令：{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raw = exc.stderr or exc.stdout or b""
        detail = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        suffix = f"：{detail[-1000:]}" if detail else ""
        raise CommandError(
            f"命令执行失败（{exc.returncode}）：{command[0]}{suffix}",
            reported=not capture,
        ) from exc


def visible_tail_lines() -> int:
    """部署尾窗约占终端高度三分之一，普通终端至少显示十行。"""
    rows = shutil.get_terminal_size(fallback=(80, 24)).lines
    return max(10, min(24, rows // 3))


class _CommandTail:
    """本地与 SSH 命令共用的有界尾窗和非 TTY 事件渲染。"""

    def __init__(self, label: str, log_path: Path | None) -> None:
        self.label = label
        self.log_path = log_path
        self.tail: deque[str] = deque(maxlen=visible_tail_lines())
        self.status = label
        self.interactive = renderer.progress_is_tty
        self.rendered = 0
        self.pending = ""
        self.width = max(20, shutil.get_terminal_size(fallback=(80, 24)).columns - 4)

    def _redraw(self) -> None:
        lines = [fit_terminal(self.status, self.width), *(
            fit_terminal(line, self.width) for line in self.tail
        )]
        if self.rendered:
            sys.stderr.write(f"\x1b[{self.rendered}F")
        height = max(self.rendered, len(lines))
        for index in range(height):
            sys.stderr.write("\x1b[2K")
            if index < len(lines):
                sys.stderr.write(lines[index])
            sys.stderr.write("\n")
        sys.stderr.flush()
        self.rendered = height

    def start(self) -> None:
        if self.interactive:
            self._redraw()
        else:
            suffix = f"（详细日志：{self.log_path}）" if self.log_path else ""
            renderer.progress(f"{self.label}{suffix}")

    def feed(self, value: str) -> None:
        self.pending += value.replace("\r", "\n")
        parts = self.pending.split("\n")
        self.pending = parts.pop()
        for line in parts:
            self._line(line)

    def close_input(self) -> None:
        if self.pending:
            self._line(self.pending)
            self.pending = ""

    def _line(self, line: str) -> None:
        if not line:
            return
        if STEP_RE.fullmatch(line):
            self.status = line
        else:
            self.tail.append(line)
        if self.interactive:
            self._redraw()

    def _clear(self) -> None:
        if not self.interactive or not self.rendered:
            return
        sys.stderr.write(f"\x1b[{self.rendered}F")
        for _ in range(self.rendered):
            sys.stderr.write("\x1b[2K\n")
        sys.stderr.write(f"\x1b[{self.rendered}F")
        sys.stderr.flush()
        self.rendered = 0

    def finish(self, returncode: int) -> None:
        if returncode == 0:
            self._clear()
            renderer.progress(f"{self.label}：{self.tail[-1] if self.tail else '完成'}")
            return
        if not self.interactive:
            if self.status != self.label:
                renderer.progress(self.status)
            for line in self.tail:
                renderer.progress(line)
        detail = f"完整输出见 {self.log_path}" if self.log_path else "错误详情见上方输出"
        renderer.error(f"{self.label}失败；{detail}")


def run_logged(
    arguments: Sequence[str], *, input_data: bytes | None, log_path: Path,
    label: str, check: bool = True,
) -> subprocess.CompletedProcess:
    """记录完整输出；TTY 中只绘制随视口缩放的实时尾部。"""
    command = [str(item) for item in arguments]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    view = _CommandTail(label, log_path)
    view.start()
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
        )
    except FileNotFoundError as exc:
        raise CommandError(f"找不到命令：{command[0]}") from exc

    assert process.stdin is not None
    assert process.stdout is not None
    try:
        if input_data is not None:
            process.stdin.write(input_data.decode("utf-8", "replace"))
        process.stdin.close()
    except BrokenPipeError:
        pass
    with log_path.open("a", encoding="utf-8") as log:
        log.write(
            f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
            f"{label}\n$ {shell_join(command)}\n"
        )
        for raw in process.stdout:
            log.write(raw)
            log.flush()
            view.feed(raw)
    view.close_input()
    process.stdout.close()
    returncode = process.wait()
    view.finish(returncode)
    result = subprocess.CompletedProcess(command, returncode)
    if check and returncode:
        raise CommandError(
            f"命令执行失败（{returncode}）：{command[0]}；详见 {log_path}",
            reported=True,
        )
    return result


def shell_join(arguments: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(item)) for item in arguments)


def target(user: str, host: str) -> str:
    if not user or not HOST_RE.fullmatch(host):
        raise CommandError("SSH 用户或主机无效")
    return f"{user}@{host}"


def _paramiko():
    try:
        import paramiko
    except ModuleNotFoundError as exc:
        raise CommandError(
            "缺少 Paramiko；源码用户请执行 python3 -m pip install -r terminal/requirements.txt"
        ) from exc
    return paramiko


def _split_destination(destination: str) -> tuple[str, str]:
    if destination.count("@") != 1:
        raise CommandError("SSH 目标必须是 user@host")
    user, host = destination.split("@", 1)
    if not user or not HOST_RE.fullmatch(host):
        raise CommandError("SSH 用户或主机无效")
    return user, host


def _connect(destination: str, identity: Path | None = None):
    paramiko = _paramiko()
    user, host = _split_destination(destination)
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts = Path.home() / ".ssh/known_hosts"
    if known_hosts.is_file():
        client.load_host_keys(str(known_hosts))

    class ConfirmHostKey(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, ssh_client, hostname, key):
            fingerprint = base64.b64encode(
                hashlib.sha256(key.asbytes()).digest()
            ).decode().rstrip("=")
            if not sys.stdin.isatty():
                raise CommandError(
                    f"未知 SSH 主机密钥：{hostname}（SHA256:{fingerprint}）；"
                    "请在交互终端首次连接并确认"
                )
            answer = input(
                f"首次连接 {hostname}，主机密钥为 SHA256:{fingerprint}\n"
                "确认信任并保存？输入 yes："
            ).strip()
            if answer != "yes":
                raise CommandError("已取消保存 SSH 主机密钥")
            known_hosts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ssh_client.get_host_keys().add(hostname, key.get_name(), key)
            ssh_client.save_host_keys(str(known_hosts))

    client.set_missing_host_key_policy(ConfirmHostKey())
    try:
        client.connect(
            host, username=user,
            key_filename=str(identity.expanduser()) if identity else None,
            allow_agent=True, look_for_keys=True,
            timeout=15, banner_timeout=15, auth_timeout=15,
        )
    except Exception as exc:
        client.close()
        raise CommandError(f"SSH 连接失败：{destination}：{exc}") from exc
    return client


def _execute(
    destination: str, arguments: Sequence[str], *, input_text: str | None,
    capture: bool, tty: bool, check: bool, identity: Path | None,
) -> subprocess.CompletedProcess:
    command = shell_join(arguments)
    client = _connect(destination, identity)
    try:
        channel = client.get_transport().open_session()
        if tty:
            channel.get_pty()
        channel.exec_command(command)
        if input_text is not None:
            channel.sendall(input_text.replace("\r\n", "\n").encode())
        channel.shutdown_write()
        stdout = bytearray()
        stderr = bytearray()
        while True:
            progressed = False
            if channel.recv_ready():
                data = channel.recv(65536)
                if capture:
                    stdout.extend(data)
                else:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                progressed = True
            if channel.recv_stderr_ready():
                data = channel.recv_stderr(65536)
                if capture:
                    stderr.extend(data)
                else:
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
                progressed = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if not progressed:
                time.sleep(0.02)
        returncode = channel.recv_exit_status()
    except Exception as exc:
        raise CommandError(f"SSH 命令执行失败：{destination}：{exc}") from exc
    finally:
        client.close()
    result = subprocess.CompletedProcess(
        list(arguments), returncode, bytes(stdout), bytes(stderr),
    )
    if check and returncode:
        raw = stderr or stdout
        detail = bytes(raw).decode("utf-8", "replace").strip()
        suffix = f"：{detail[-1000:]}" if detail else ""
        raise CommandError(
            f"远端命令退出（{returncode}）{suffix}", reported=not capture,
        )
    return result


def ssh(
    destination: str, arguments: Sequence[str], *, input_text: str | None = None,
    capture: bool = False, tty: bool = False, check: bool = True,
    identity: Path | None = None,
) -> subprocess.CompletedProcess:
    return _execute(
        destination, arguments, input_text=input_text, capture=capture,
        tty=tty, check=check, identity=identity,
    )


def ssh_logged(
    destination: str, arguments: Sequence[str], *, input_text: str | None,
    log_path: Path | None, label: str, check: bool = True,
    tty: bool = False,
) -> subprocess.CompletedProcess:
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    view = _CommandTail(label, log_path)
    view.start()

    command = shell_join(arguments)
    client = _connect(destination)
    returncode = 255
    try:
        channel = client.get_transport().open_session()
        if tty:
            channel.get_pty()
        channel.exec_command(command)
        if input_text is not None:
            channel.sendall(input_text.replace("\r\n", "\n").encode())
        channel.shutdown_write()
        log_context = (
            log_path.open("a", encoding="utf-8")
            if log_path is not None else contextlib.nullcontext(None)
        )
        with log_context as log:
            if log is not None:
                log.write(
                    f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                    f"{label}\n$ {destination} {command}\n"
                )
            while True:
                chunks: list[bytes] = []
                if channel.recv_ready():
                    chunks.append(channel.recv(65536))
                if channel.recv_stderr_ready():
                    chunks.append(channel.recv_stderr(65536))
                for data in chunks:
                    text = data.decode("utf-8", "replace")
                    if log is not None:
                        log.write(text)
                        log.flush()
                    view.feed(text)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    view.close_input()
                    break
                if not chunks:
                    time.sleep(0.02)
            returncode = channel.recv_exit_status()
    except Exception as exc:
        raise CommandError(f"SSH 命令执行失败：{destination}：{exc}") from exc
    finally:
        client.close()

    result = subprocess.CompletedProcess(list(arguments), returncode)
    view.finish(returncode)
    if returncode:
        if check:
            detail = f"；详见 {log_path}" if log_path is not None else ""
            raise CommandError(f"远端命令退出（{returncode}）{detail}", reported=True)
    return result


def scp(source: Path, destination: str) -> None:
    if ":" not in destination:
        raise CommandError("上传目标必须是 user@host:/absolute/path")
    host, remote_path = destination.split(":", 1)
    upload_file(source, host, remote_path)


def _mkdir_sftp(sftp, directory: str) -> None:
    path = PurePosixPath(directory)
    current = PurePosixPath("/")
    for part in path.parts[1:]:
        current /= part
        try:
            info = sftp.stat(current.as_posix())
            if not stat.S_ISDIR(info.st_mode):
                raise CommandError(f"远端路径不是目录：{current}")
        except OSError:
            sftp.mkdir(current.as_posix())


def _remote_path(value: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise CommandError(f"远端路径必须是安全的绝对路径：{value}")
    return path.as_posix()


def _atomic_replace(client, sftp, temporary: str, final: str) -> None:
    try:
        sftp.posix_rename(temporary, final)
        return
    except OSError:
        pass
    command = shell_join([
        "python3", "-c", "import os,sys;os.replace(sys.argv[1],sys.argv[2])",
        temporary, final,
    ])
    _, stdout, stderr = client.exec_command(command)
    returncode = stdout.channel.recv_exit_status()
    if returncode:
        detail = stderr.read().decode("utf-8", "replace").strip()
        raise CommandError(f"远端原子替换失败：{detail or final}")


def remote_file_exists(destination: str, remote_path: str) -> bool:
    remote_path = _remote_path(remote_path)
    client = _connect(destination)
    try:
        sftp = client.open_sftp()
        try:
            sftp.stat(remote_path)
        except IOError:
            return False
        return True
    finally:
        client.close()


def download_file(
    destination: str, remote_path: str, local_path: Path, *,
    mode: int | None = None,
) -> None:
    """通过 SFTP 下载单个文件，并原子替换本地目标。"""
    remote_path = _remote_path(remote_path)
    local_path = Path(local_path).resolve()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    client = _connect(destination)
    temporary = local_path.with_name(local_path.name + ".tmp")
    try:
        sftp = client.open_sftp()
        with sftp.open(remote_path, "rb") as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, local_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise CommandError(f"下载远端文件失败：{remote_path}：{exc}") from exc
    finally:
        client.close()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_file(
    source: Path, destination: str, remote_path: str, *, mode: int | None = None,
) -> bool:
    """通过 SFTP 原子上传单个文件；内容相同时跳过。"""
    source = source.resolve()
    remote_path = _remote_path(remote_path)
    if not source.is_file() or source.is_symlink():
        raise CommandError(f"上传源不是普通文件：{source}")
    client = _connect(destination)
    try:
        sftp = client.open_sftp()
        _mkdir_sftp(sftp, str(PurePosixPath(remote_path).parent))
        digest = _sha256_file(source)
        unchanged = False
        try:
            with sftp.open(remote_path, "rb") as remote_file:
                remote_digest = hashlib.sha256()
                while True:
                    chunk = remote_file.read(1024 * 1024)
                    if not chunk:
                        break
                    remote_digest.update(chunk)
                unchanged = remote_digest.hexdigest() == digest
        except OSError:
            pass
        wanted_mode = mode if mode is not None else stat.S_IMODE(source.stat().st_mode)
        if unchanged:
            sftp.chmod(remote_path, wanted_mode)
            return False
        temporary = f"{remote_path}.mcsp-new-{uuid.uuid4().hex}"
        try:
            sftp.put(str(source), temporary)
            sftp.chmod(temporary, wanted_mode)
            _atomic_replace(client, sftp, temporary, remote_path)
        finally:
            try:
                sftp.remove(temporary)
            except OSError:
                pass
        return True
    except CommandError:
        raise
    except Exception as exc:
        raise CommandError(f"SFTP 上传失败：{source} -> {destination}:{remote_path}：{exc}") from exc
    finally:
        client.close()


def _ignored(relative: str, patterns: Sequence[str]) -> bool:
    from fnmatch import fnmatch
    matched = False
    path = PurePosixPath(relative)
    candidates = [path.as_posix(), *(item.as_posix() for item in path.parents)]
    for pattern in patterns:
        negate = pattern.startswith("!")
        rule = pattern[1:] if negate else pattern
        if any(fnmatch(item, rule) or fnmatch(item + "/", rule) for item in candidates):
            matched = not negate
    return matched


_REMOTE_MANIFEST = r'''import hashlib,json,os,stat,sys
root,preserved_json,merged_json=sys.argv[1:]
preserved=set(json.loads(preserved_json)); merged=tuple(json.loads(merged_json))
files={}; dirs=[]
def managed(rel):
    top=rel.split('/',1)[0]
    return top not in preserved or any(rel == item or rel.startswith(item + '/') for item in merged)
if os.path.isdir(root):
  for base, names, filenames in os.walk(root, topdown=True, followlinks=False):
    relbase=os.path.relpath(base,root).replace(os.sep,'/')
    relbase='' if relbase=='.' else relbase
    kept=[]
    for name in names:
      rel=f'{relbase}/{name}'.lstrip('/')
      path=os.path.join(base,name)
      if managed(rel):
        if os.path.islink(path): raise SystemExit('受管目录包含符号链接：'+rel)
        dirs.append(rel); kept.append(name)
      elif any(item == rel or item.startswith(rel + '/') for item in merged): kept.append(name)
    names[:]=kept
    for name in filenames:
      rel=f'{relbase}/{name}'.lstrip('/')
      if not managed(rel): continue
      path=os.path.join(base,name)
      if os.path.islink(path) or not os.path.isfile(path): raise SystemExit('受管路径不是普通文件：'+rel)
      digest=hashlib.sha256()
      with open(path,'rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''): digest.update(chunk)
      files[rel]=[digest.hexdigest(),stat.S_IMODE(os.stat(path).st_mode)]
print(json.dumps({'files':files,'dirs':dirs},separators=(',',':')))
'''


def sync_tree(
    source: Path, destination: str, remote_root: str, *,
    preserve: Sequence[str] = (), exclude: Sequence[str] = (),
) -> tuple[int, int]:
    """将受管源码按 SHA-256 文件级增量同步到远端。"""
    source = source.resolve()
    remote_root = _remote_path(remote_root)
    if not source.is_dir():
        raise CommandError(f"同步源目录不存在：{source}")
    patterns: list[str] = list(exclude)
    ignore = source / ".mcspignore"
    if ignore.is_file():
        patterns.extend(
            line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    files: dict[str, tuple[Path, str, int]] = {}
    dirs: set[str] = set()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.relative_to(source).parts or path.suffix == ".pyc" or _ignored(relative, patterns):
            continue
        if path.is_symlink():
            raise CommandError(f"同步源不能包含符号链接：{relative}")
        if path.is_dir():
            dirs.add(relative)
        elif path.is_file():
            digest = _sha256_file(path)
            files[relative] = (path, digest, stat.S_IMODE(path.stat().st_mode))
    preserved = set(preserve)
    merged = sorted({
        "/".join(relative.split("/")[:2])
        for relative in (*dirs, *files)
        if relative.split("/", 1)[0] in preserved and "/" in relative
    })
    result = ssh(
        destination,
        ["python3", "-c", _REMOTE_MANIFEST, remote_root,
         json.dumps(sorted(preserved)), json.dumps(merged)],
        capture=True,
    )
    try:
        current = json.loads((result.stdout or b"{}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError("远端 Python 未返回有效的同步清单") from exc
    remote_files = current.get("files", {})
    remote_dirs = set(current.get("dirs", ()))
    changed = [name for name, (_, digest, mode) in files.items()
               if remote_files.get(name) != [digest, mode]]
    client = _connect(destination)
    try:
        sftp = client.open_sftp()
        _mkdir_sftp(sftp, remote_root)
        stale = set(remote_files) - set(files)
        required_dirs = dirs | {
            str(PurePosixPath(name).parent) for name in files if "/" in name
        }
        blocking_files = {
            name for name in stale
            if name in required_dirs
            or any(directory.startswith(name + "/") for directory in required_dirs)
        }
        for name in sorted(blocking_files, reverse=True):
            sftp.remove(f"{remote_root}/{name}")
        blocking_dirs = {
            directory for directory in remote_dirs
            if directory in files
            or any(directory.startswith(name + "/") for name in files)
        }
        for name in sorted(
            stale, key=lambda item: (-item.count("/"), item),
        ):
            if any(name.startswith(directory + "/") for directory in blocking_dirs):
                sftp.remove(f"{remote_root}/{name}")
                blocking_files.add(name)
        for name in sorted(blocking_dirs, key=lambda item: (-item.count("/"), item)):
            try:
                sftp.rmdir(f"{remote_root}/{name}")
            except OSError:
                pass
        for directory in sorted(dirs, key=lambda item: (item.count("/"), item)):
            _mkdir_sftp(sftp, f"{remote_root}/{directory}")
        for name in changed:
            local, _, mode = files[name]
            final = f"{remote_root}/{name}"
            temporary = f"{final}.mcsp-new-{uuid.uuid4().hex}"
            _mkdir_sftp(sftp, str(PurePosixPath(final).parent))
            sftp.put(str(local), temporary)
            sftp.chmod(temporary, mode)
            try:
                _atomic_replace(client, sftp, temporary, final)
            finally:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
        remaining_stale = sorted(stale - blocking_files, reverse=True)
        for name in remaining_stale:
            sftp.remove(f"{remote_root}/{name}")
        wanted_dirs = required_dirs
        stale_dirs = sorted(remote_dirs - wanted_dirs,
                            key=lambda item: (-item.count("/"), item))
        for name in stale_dirs:
            try: sftp.rmdir(f"{remote_root}/{name}")
            except OSError: pass
    except Exception as exc:
        raise CommandError(f"SFTP 增量同步失败：{destination}:{remote_root}：{exc}") from exc
    finally:
        client.close()
    return len(changed), len(set(remote_files) - set(files))


class Transport:
    """供部署与整合包服务注入的 SSH/SCP 边界。"""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path

    def ssh(self, destination: str, arguments: list[str], **kwargs):
        return ssh(destination, arguments, **kwargs)

    def ssh_logged(
        self, destination: str, arguments: list[str], *, label: str,
        input_text: str | None = None, check: bool = True,
    ):
        return ssh_logged(
            destination, arguments, input_text=input_text,
            log_path=self.log_path, label=label, check=check,
        )

    def ssh_tail(
        self, destination: str, arguments: list[str], *, label: str,
        input_text: str | None = None, check: bool = True,
        tty: bool = False,
    ):
        return ssh_logged(
            destination, arguments, input_text=input_text,
            log_path=None, label=label, check=check, tty=tty,
        )

    def scp(self, source: Path, destination: str) -> None:
        scp(source, destination)

    def upload_file(self, source: Path, destination: str, remote_path: str, *, mode: int | None = None) -> bool:
        return upload_file(source, destination, remote_path, mode=mode)

    def download_file(
        self, destination: str, remote_path: str, local_path: Path, *,
        mode: int | None = None,
    ) -> None:
        download_file(destination, remote_path, local_path, mode=mode)

    def remote_file_exists(self, destination: str, remote_path: str) -> bool:
        return remote_file_exists(destination, remote_path)

    def sync_tree(
        self, source: Path, destination: str, remote_root: str, *,
        preserve: Sequence[str] = (), exclude: Sequence[str] = (),
    ) -> tuple[int, int]:
        return sync_tree(
            source, destination, remote_root,
            preserve=preserve, exclude=exclude,
        )
