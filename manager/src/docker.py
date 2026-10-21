"""Manager 对共享 Docker SDK 的配置适配。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .config import MANAGER_ROOT, render_compose, runtime_image
from .docker_sdk import (
    DockerClient as SharedDockerClient,
    DockerError,
    EventSink,
    Runner,
    discard_event,
)


_discard_event = discard_event


class DockerClient(SharedDockerClient):
    def __init__(
        self, manager_root: Path = MANAGER_ROOT, runner: Runner = subprocess.run,
        events: EventSink = discard_event,
    ) -> None:
        super().__init__(
            manager_root, image_resolver=runtime_image,
            compose_renderer=render_compose, runner=runner, events=events,
        )
