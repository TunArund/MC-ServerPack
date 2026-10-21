"""Terminal 专用的远程 Docker 环境准备。"""

from __future__ import annotations

import sys
from pathlib import Path

from . import remote
from .docker_sdk import DockerProvisioner as SharedDockerProvisioner


def _resource(name: str) -> Path:
    """Locate a source resource or a resource in the replaceable data tree."""
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).resolve().parent / "data" / name
        if bundled.is_file():
            return bundled
    source = Path(__file__).resolve().with_name(name)
    if source.is_file():
        return source
    raise RuntimeError(f"缺少 Terminal 资源文件：{name}")


class DockerProvisioner(SharedDockerProvisioner):
    """为共享 Provisioner 注入 Terminal 资源与 SSH 目标。"""

    def __init__(
        self, host: str, user: str, *, transport: remote.Transport | None = None,
    ) -> None:
        script = _resource("docker-install.sh").read_text(encoding="utf-8")
        super().__init__(
            remote.target(user, host), script, transport or remote.Transport(),
        )
