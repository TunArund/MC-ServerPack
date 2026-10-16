"""Normalize QQ events and enforce configured target/admin boundaries."""

from __future__ import annotations

import re
from dataclasses import dataclass


MENTION_RE = re.compile(r"^(?:<@!?[^>]+>\s*)+")


@dataclass(frozen=True)
class Target:
    kind: str
    identifier: str


@dataclass(frozen=True)
class Incoming:
    message_id: str
    target: Target
    sender_id: str
    content: str


class TargetPolicy:
    def __init__(
        self, targets: tuple[Target, ...], admins: tuple[str, ...],
        alert_routes: dict[str, frozenset[Target]] | None = None,
    ) -> None:
        self.targets = frozenset(targets)
        self.admins = frozenset(admins)
        self.alert_routes = alert_routes or {}

    def allows(self, incoming: Incoming) -> bool:
        return incoming.target in self.targets

    def is_admin(self, incoming: Incoming) -> bool:
        return incoming.sender_id in self.admins

    def alert_targets(self, kind: str) -> frozenset[Target]:
        return self.alert_routes.get(kind, frozenset())


def command_text(content: str) -> str:
    return MENTION_RE.sub("", content.strip()).strip()


def incoming(event_name: str, event: dict) -> Incoming | None:
    author = event.get("author") or {}
    if event_name == "GROUP_AT_MESSAGE_CREATE":
        kind = "group"
        target_id = str(event.get("group_openid") or "")
        sender_id = str(
            author.get("member_openid") or event.get("group_member_openid") or ""
        )
    elif event_name == "C2C_MESSAGE_CREATE":
        kind = "user"
        sender_id = str(author.get("user_openid") or "")
        target_id = sender_id
    else:
        return None
    message_id = str(event.get("id") or "")
    if not message_id or not target_id or not sender_id:
        return None
    return Incoming(
        message_id,
        Target(kind, target_id),
        sender_id,
        command_text(str(event.get("content") or "")),
    )
