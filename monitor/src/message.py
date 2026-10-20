"""从 YAML 渲染 QQ 文案与按钮，不包含网络和业务逻辑。"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import yaml

from .network import Outgoing


TEMPLATE_FILE = Path(__file__).with_name("message.yml")
LOGGER = logging.getLogger(__name__)


def message_path() -> Path:
    explicit = os.environ.get("MCSP_MESSAGES")
    if explicit:
        return Path(explicit)
    data = os.environ.get("MCSP_DATA")
    return Path(data) / "message.yml" if data else TEMPLATE_FILE


class MessageError(RuntimeError):
    pass


class MessageRenderer:
    def __init__(
        self, path: Path | None = None, *,
        services: set[str] | frozenset[str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else message_path()
        self.services = frozenset(services) if services is not None else None
        self.actions: dict[str, str] = {}
        self.templates: dict[str, dict] = {}
        self._commands: dict[str, str] = {}
        self._signature: tuple[int, int] | None = None
        self._prepare_path()
        self._load()

    def _prepare_path(self) -> None:
        if self.path == TEMPLATE_FILE or self.path.exists():
            return
        if not TEMPLATE_FILE.is_file():
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(TEMPLATE_FILE, self.path)
            LOGGER.info("已初始化 QQ 消息模板：%s", self.path)
        except OSError:
            LOGGER.exception("无法初始化 QQ 消息模板，回退到内置模板")
            self.path = TEMPLATE_FILE

    def _signature_of(self) -> tuple[int, int] | None:
        try:
            status = self.path.stat()
        except OSError:
            return None
        return (status.st_mtime_ns, status.st_size)

    def _load(self) -> None:
        try:
            document = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise MessageError(f"QQ 消息模板读取失败：{exc}") from exc
        if not isinstance(document, dict):
            raise MessageError("QQ 消息模板必须是对象")
        document = dict(document)
        raw_actions = document.pop("keyboard", None)
        if not isinstance(raw_actions, dict) or not raw_actions:
            raise MessageError("QQ 消息模板缺少 keyboard 服务表")
        if not all(
            isinstance(name, str) and name
            and isinstance(label, str) and label.strip()
            for name, label in raw_actions.items()
        ):
            raise MessageError("keyboard 必须是非空的“服务 ID: 按钮文案”对象")
        actions = {name: label.strip() for name, label in raw_actions.items()}
        labels = list(actions.values())
        if len(labels) != len(set(labels)):
            raise MessageError("keyboard 按钮文案不能重复")
        if self.services is not None:
            unknown_services = set(actions) - self.services
            if unknown_services:
                raise MessageError(
                    "keyboard 引用了未实现服务："
                    f"{', '.join(sorted(unknown_services))}"
                )

        templates: dict[str, dict] = {}
        for name, template in document.items():
            if not isinstance(name, str) or not isinstance(template, dict):
                raise MessageError(f"消息 {name!r} 必须是对象")
            text = template.get("text")
            markdown = template.get("markdown")
            if (isinstance(text, str)) == (isinstance(markdown, str)):
                raise MessageError(f"消息 {name} 必须且只能配置 text 或 markdown")
            rows = template.get("keyboard", [])
            if not isinstance(rows, list) or not all(
                isinstance(row, list) and row
                and all(isinstance(action, str) for action in row)
                for row in rows
            ):
                raise MessageError(f"消息 {name} 的 keyboard 必须是二维服务 ID 数组")
            if rows and not isinstance(markdown, str):
                raise MessageError(
                    f"消息 {name} 含按钮时必须使用 markdown；"
                    "QQ 客户端不会为纯文本消息显示自定义键盘"
                )
            for row in rows:
                if len(row) > 5:
                    raise MessageError(f"消息 {name} 的单行按钮不能超过 5 个")
                unknown = [action for action in row if action not in actions]
                if unknown:
                    raise MessageError(
                        f"消息 {name} 引用了未知服务：{', '.join(unknown)}"
                    )
            templates[name] = template

        self.actions = actions
        self._commands = {label: name for name, label in actions.items()}
        self.templates = templates
        self._signature = self._signature_of()

    def set_services(self, services: set[str] | frozenset[str]) -> None:
        self.services = frozenset(services)
        unknown = set(self.actions) - self.services
        if unknown:
            raise MessageError(
                f"keyboard 引用了未实现服务：{', '.join(sorted(unknown))}"
            )

    def reload_if_changed(self) -> bool:
        signature = self._signature_of()
        if signature is None or signature == self._signature:
            return False
        try:
            self._load()
        except MessageError as exc:
            # Avoid retrying the same broken file on every message.
            self._signature = signature
            LOGGER.warning("QQ 消息模板热加载失败，继续使用旧模板：%s", exc)
            return False
        LOGGER.info("QQ 消息模板已热加载：%s", self.path)
        return True

    def action(self, command: str) -> str | None:
        self.reload_if_changed()
        return self._commands.get(command)

    def render(
        self, template_name: str, *, button_data: dict[str, str] | None = None,
        **values: object,
    ) -> Outgoing:
        self.reload_if_changed()
        try:
            template = self.templates[template_name]
        except KeyError as exc:
            raise MessageError(f"缺少 QQ 消息模板：{template_name}") from exc
        markdown = "markdown" in template
        try:
            content = template["markdown" if markdown else "text"].format(**values)
        except KeyError as exc:
            raise MessageError(f"QQ 消息模板 {template_name} 缺少参数：{exc.args[0]}") from exc
        rows = template.get("keyboard", [])
        keyboard = None
        if rows:
            keyboard = {"content": {"rows": [
                {"buttons": [
                    self._button(action, (button_data or {}).get(action))
                    for action in row
                ]}
                for row in rows
            ]}}
        return Outgoing(content, markdown, keyboard)

    def _button(self, action: str, data: str | None = None) -> dict:
        label = self.actions[action]
        return {
            "id": action,
            "render_data": {
                "label": label,
                "visited_label": label,
                "style": 1,
            },
            "action": {
                "type": 2,
                "permission": {"type": 2},
                "data": data or label,
                "enter": True,
                "unsupport_tips": f"请直接发送「{label}」",
            },
        }

    def choices(
        self, template_name: str, service: str,
        choices: list[tuple[str, str]], **values: object,
    ) -> Outgoing:
        """Render a template with runtime buttons as ``(label, command)`` pairs."""
        message = self.render(template_name, **values)
        buttons = []
        for index, (label, command) in enumerate(choices[:10]):
            button = self._button(service, command)
            button["id"] = f"{service}-{index}"
            button["render_data"]["label"] = label[:10]
            button["render_data"]["visited_label"] = label[:10]
            buttons.append(button)
        keyboard = {"content": {"rows": [
            {"buttons": buttons[index:index + 2]}
            for index in range(0, len(buttons), 2)
        ]}}
        return Outgoing(message.content, True, keyboard)


messages = MessageRenderer()
