"""Precise edits to Monitor-owned configuration in the Terminal config."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml

from ..config import ConfigError, TARGET_NAME_RE, load


def upsert_user_target(path: str | Path, name: str, identifier: str) -> None:
    """Update one named user target without reformatting unrelated YAML."""
    if not TARGET_NAME_RE.fullmatch(name):
        raise ConfigError("target name 仅允许大小写字母、数字和减号")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ConfigError("user_openid 必须是非空字符串")
    identifier = identifier.strip()
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    document = yaml.compose(text)

    def pair(node, key: str):
        if node is None or node.id != "mapping":
            raise ConfigError(f"缺少 monitor.qqbot.{key}")
        for item_key, value in node.value:
            if item_key.value == key:
                return item_key, value
        raise ConfigError(f"缺少 monitor.qqbot.{key}")

    _, monitor = pair(document, "monitor")
    _, qqbot = pair(monitor, "qqbot")
    targets_key, targets = pair(qqbot, "targets")
    if targets.id != "sequence":
        raise ConfigError("monitor.qqbot.targets 必须是数组")

    quoted_id = "'" + identifier.replace("'", "''") + "'"
    updated = None
    for item in targets.value:
        if item.id != "mapping":
            continue
        fields = {key.value: value for key, value in item.value}
        if fields.get("name") is None or fields["name"].value != name:
            continue
        if fields.get("type") is None or fields["type"].value != "user":
            raise ConfigError(f"monitor.qqbot.targets.{name} 不是 user 目标")
        target_id = fields.get("id")
        if target_id is None:
            raise ConfigError(f"monitor.qqbot.targets.{name} 缺少 id")
        updated = (
            text[:target_id.start_mark.index]
            + quoted_id
            + text[target_id.end_mark.index:]
        )
        break

    if updated is None:
        indent = " " * targets_key.start_mark.column
        entry = (
            f"{indent}- name: {name}\n"
            f"{indent}  type: user\n"
            f"{indent}  id: {quoted_id}\n"
        )
        if targets.flow_style:
            updated = (
                text[:targets.start_mark.index]
                + "\n" + entry
                + text[targets.end_mark.index:]
            )
        else:
            insertion = text.rfind("\n", 0, targets.end_mark.index) + 1
            updated = text[:insertion] + entry + text[insertion:]

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=source.parent,
            prefix=".config-", suffix=".yml", delete=False,
        ) as output:
            output.write(updated)
            temporary = Path(output.name)
        os.chmod(temporary, source.stat().st_mode & 0o777)
        load(temporary)
        os.replace(temporary, source)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
