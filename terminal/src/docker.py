"""Terminal 专用的远程 Docker 环境准备。"""

from __future__ import annotations

import sys
from pathlib import Path

from . import remote


def _resource(name: str) -> Path:
    """Locate a source resource or a resource bundled by a one-file release."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled = Path(bundle_root) / "terminal/src" / name
        if bundled.is_file():
            return bundled
    source = Path(__file__).resolve().with_name(name)
    if source.is_file():
        return source
    raise RuntimeError(f"缺少 Terminal 资源文件：{name}")


class DockerProvisioner:
    """Terminal 直接在目标服务器安装 Docker，不依赖运行时源码。"""

    def __init__(
        self, host: str, user: str, *, transport: remote.Transport | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.transport = transport or remote.Transport()

    def install(self) -> None:
        script = _resource("docker-install.sh").read_text(encoding="utf-8")
        self.transport.ssh_logged(
            remote.target(self.user, self.host),
            ["bash", "-s", "--"], input_text=script,
            label=f"准备 Docker：{self.user}@{self.host}",
        )
