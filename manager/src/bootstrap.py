"""Manager 首次安装：生成 API 密钥和私有 TLS 证书。

由 ``manager.sh install`` 在控制容器启动前调用，因此 Manager 可以完全独立部署，
不依赖宿主机 Python 或 openssl；镜像内已安装 openssl。
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import MANAGER_ROOT


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class CertificateFiles:
    ca: Path | None
    certificate: Path
    private_key: Path
    created: bool = False


def ensure_api_key(env_path: Path) -> tuple[str, bool]:
    """确保 data/.env 中有 32 字符以上的 MANAGER_API_KEY。"""
    env_path = Path(env_path)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        raise BootstrapError(f"无法读取 {env_path}：{exc}") from exc

    key = ""
    generated = False
    output: list[str] = []
    for line in lines:
        if line.startswith("MANAGER_API_KEY="):
            value = line.split("=", 1)[1].strip().strip("\"'")
            if len(value) >= 32:
                key = value
                output.append(line)
                continue
            key = secrets.token_urlsafe(48)
            generated = True
            output.append(f"MANAGER_API_KEY={key}")
            continue
        output.append(line)
    if not key:
        key = secrets.token_urlsafe(48)
        generated = True
        output.append(f"MANAGER_API_KEY={key}")
    if not generated:
        return key, False

    env_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = env_path.with_name(env_path.name + ".tmp")
    try:
        temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, env_path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise BootstrapError(f"无法写入 {env_path}：{exc}") from exc
    return key, True


def ensure_certificate(
    hosts: tuple[str, ...], directory: Path,
) -> CertificateFiles:
    """生成由私有 CA 签发的服务端证书；已有证书时原样复用。"""
    directory = Path(directory)
    certificate = directory / "server.crt"
    private_key = directory / "server.key"
    ca = directory / "ca.crt"
    if certificate.is_file() and private_key.is_file():
        return CertificateFiles(
            ca if ca.is_file() else None, certificate, private_key,
        )
    if certificate.exists() or private_key.exists():
        raise BootstrapError(
            f"{directory} 中的 server.crt/server.key 不完整；请删除后重新安装"
        )

    host_list = tuple(dict.fromkeys(host for host in hosts if host))
    if not host_list:
        raise BootstrapError("无法生成 TLS 证书：Manager host 为空")
    openssl = shutil.which("openssl")
    if openssl is None:
        raise BootstrapError("镜像缺少 openssl，无法生成 Manager TLS 证书")
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".tls-", dir=directory.parent,
        ) as temporary:
            staging = Path(temporary)
            san = ",".join(
                f"IP:{host}" if _is_ip(host) else f"DNS:{host}"
                for host in host_list
            )
            extension = staging / "server.ext"
            extension.write_text(
                "\n".join((
                    "[v3_server]",
                    "basicConstraints=critical,CA:FALSE",
                    "keyUsage=critical,digitalSignature,keyEncipherment",
                    "extendedKeyUsage=serverAuth",
                    f"subjectAltName={san}",
                )) + "\n",
                encoding="utf-8",
            )
            commands = (
                (
                    "生成 Manager CA",
                    (
                        openssl, "req", "-x509", "-newkey", "rsa:3072",
                        "-sha256", "-days", "3650", "-nodes",
                        "-keyout", str(staging / "ca.key"),
                        "-out", str(staging / "ca.crt"),
                        "-subj", "/CN=MC-ServerPack Manager CA",
                    ),
                ),
                (
                    "生成 Manager 服务端私钥",
                    (
                        openssl, "req", "-newkey", "rsa:3072", "-sha256",
                        "-nodes", "-keyout", str(staging / "server.key"),
                        "-out", str(staging / "server.csr"),
                        "-subj", f"/CN={host_list[0]}",
                    ),
                ),
                (
                    "签发 Manager 服务端证书",
                    (
                        openssl, "x509", "-req",
                        "-in", str(staging / "server.csr"),
                        "-CA", str(staging / "ca.crt"),
                        "-CAkey", str(staging / "ca.key"),
                        "-CAcreateserial",
                        "-out", str(staging / "server.crt"),
                        "-days", "3650", "-sha256",
                        "-extfile", str(extension),
                        "-extensions", "v3_server",
                    ),
                ),
            )
            for label, command in commands:
                try:
                    subprocess.run(
                        command, check=True, capture_output=True, text=True,
                    )
                except subprocess.CalledProcessError as exc:
                    detail = (exc.stderr or exc.stdout or "").strip()
                    raise BootstrapError(
                        f"{label}失败：{detail[-800:]}",
                    ) from exc
                except OSError as exc:
                    raise BootstrapError(f"{label}失败：{exc}") from exc

            (staging / "server.key").chmod(0o600)
            (staging / "ca.key").chmod(0o600)
            (staging / "server.crt").chmod(0o644)
            (staging / "ca.crt").chmod(0o644)
            os.replace(staging / "server.crt", certificate)
            os.replace(staging / "server.key", private_key)
            os.replace(staging / "ca.crt", ca)
            os.replace(staging / "ca.key", directory / "ca.key")
    except OSError as exc:
        raise BootstrapError(f"无法生成 Manager TLS 证书：{exc}") from exc
    return CertificateFiles(ca, certificate, private_key, created=True)


def ensure(root: Path = MANAGER_ROOT) -> None:
    root = Path(root)
    config_path = root / "data/config.yml"
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError(f"缺少 {config_path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapError(f"Manager 配置读取失败：{exc}") from exc
    if not isinstance(document, dict):
        raise BootstrapError("Manager config.yml 必须是对象")
    host = document.get("host")
    if not isinstance(host, str) or not host.strip():
        raise BootstrapError("data/config.yml 缺少 host，无法生成 TLS 证书")
    _, key_generated = ensure_api_key(root / "data/.env")
    certificates = ensure_certificate((host.strip(),), root / "data/tls")
    if key_generated:
        print("已生成 MANAGER_API_KEY：data/.env", flush=True)
    if certificates.created:
        print("已生成 Manager TLS 证书：data/tls", flush=True)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def main() -> int:
    try:
        ensure()
    except BootstrapError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
