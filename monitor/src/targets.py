"""持久化 QQ 管理员、订阅目标与待审批申请。"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .network import Target


REQUEST_TTL = 24 * 3600
REQUEST_REMIND = 6 * 3600


class TargetStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class SubscriptionRequest:
    request_id: str
    kind: str
    identifier: str
    name: str
    requester: str
    requester_id: str
    message_id: str
    created_at: float
    purpose: str = "subscription"


class TargetStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.admins: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            raise TargetStateError(f"QQ 目标状态读取失败：{exc}") from exc
        if not isinstance(document, dict):
            raise TargetStateError("QQ 目标状态必须是对象")
        admins = document.get("admins", [])
        users = document.get("users", {})
        groups = document.get("groups", {})
        pending = document.get("pending", {})
        if (
            isinstance(admins, list) and admins
            and all(isinstance(item, str) for item in admins)
        ):
            raise TargetStateError(
                "检测到旧版 qqbot.json；当前版本不兼容，"
                "请删除后重新运行 add-admin"
            )
        if not isinstance(admins, list):
            raise TargetStateError("QQ admins 必须是数组")
        parsed_admins: dict[str, dict] = {}
        for index, item in enumerate(admins):
            if not isinstance(item, dict):
                raise TargetStateError(f"QQ admins[{index}] 必须是对象")
            user_openid = item.get("user-openid")
            if not isinstance(user_openid, str) or not user_openid:
                raise TargetStateError(
                    f"QQ admins[{index}].user-openid 必须是非空字符串"
                )
            union_openid = item.get("union-openid")
            if union_openid is not None and (
                not isinstance(union_openid, str) or not union_openid
            ):
                raise TargetStateError(
                    f"QQ admins[{index}].union-openid 必须是非空字符串或 null"
                )
            name = item.get("name")
            if name is not None and not isinstance(name, str):
                raise TargetStateError(f"QQ admins[{index}].name 必须是字符串")
            added_at = item.get("added-at")
            if added_at is not None and (
                not isinstance(added_at, (int, float)) or isinstance(added_at, bool)
            ):
                raise TargetStateError(f"QQ admins[{index}].added-at 必须是数字")
            group_identities = item.get("group-identities", {})
            if not isinstance(group_identities, dict) or not all(
                isinstance(group, str) and group
                and isinstance(member, str) and member
                for group, member in group_identities.items()
            ):
                raise TargetStateError(
                    f"QQ admins[{index}].group-identities 必须是群与成员 OpenID 对象"
                )
            parsed_admins[user_openid] = {
                "union-openid": union_openid,
                "name": name,
                "added-at": float(added_at) if added_at is not None else None,
                "group-identities": dict(group_identities),
            }
        self._validate_records(users, "users")
        self._validate_records(groups, "groups")
        self._validate_pending(pending)
        self.admins = parsed_admins
        self.users = dict(users)
        self.groups = dict(groups)
        self.pending = dict(pending)

    @staticmethod
    def _validate_records(value: object, field: str) -> None:
        if not isinstance(value, dict):
            raise TargetStateError(f"QQ {field} 必须是对象")
        for key, item in value.items():
            if not isinstance(key, str) or not key or not isinstance(item, dict):
                raise TargetStateError(f"QQ {field} 的条目格式无效")
            name = item.get("name")
            if name is not None and not isinstance(name, str):
                raise TargetStateError(f"QQ {field}.{key}.name 必须是字符串")
            approved_at = item.get("approved-at")
            if approved_at is not None and (
                not isinstance(approved_at, (int, float))
                or isinstance(approved_at, bool)
            ):
                raise TargetStateError(f"QQ {field}.{key}.approved-at 必须是数字")

    @staticmethod
    def _validate_pending(pending: object) -> None:
        if not isinstance(pending, dict):
            raise TargetStateError("QQ pending 必须是对象")
        for request_id, item in pending.items():
            if not isinstance(request_id, str) or not isinstance(item, dict):
                raise TargetStateError("QQ pending 的条目格式无效")
            if item.get("kind") not in ("user", "group"):
                raise TargetStateError("QQ pending.kind 必须是 user 或 group")
            if item.get("purpose", "subscription") not in (
                "subscription", "management",
            ):
                raise TargetStateError("QQ pending.purpose 无效")
            for key in ("identifier", "name", "requester", "requester-id", "message-id"):
                if not isinstance(item.get(key), str) or not item[key]:
                    raise TargetStateError(f"QQ pending.{key} 必须是非空字符串")
            for key in ("created-at", "last-notified-at"):
                value = item.get(key)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                ):
                    raise TargetStateError(f"QQ pending.{key} 必须是数字")
            union_openid = item.get("union-openid")
            if union_openid is not None and (
                not isinstance(union_openid, str) or not union_openid
            ):
                raise TargetStateError("QQ pending.union-openid 必须是字符串或 null")

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "admins": [
                {
                    "user-openid": user_openid,
                    "union-openid": record.get("union-openid"),
                    "name": record.get("name"),
                    "added-at": record.get("added-at"),
                    "group-identities": record.get("group-identities", {}),
                }
                for user_openid, record in sorted(self.admins.items())
            ],
            "users": {key: self.users[key] for key in sorted(self.users)},
            "groups": {key: self.groups[key] for key in sorted(self.groups)},
            "pending": {key: self.pending[key] for key in sorted(self.pending)},
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
        except OSError as exc:
            raise TargetStateError(f"QQ 目标状态写入失败：{exc}") from exc

    def add_admin(
        self, user_openid: str, union_openid: str | None = None, *,
        name: str | None = None, now: float | None = None,
    ) -> None:
        if not user_openid:
            raise TargetStateError("管理员 user_openid 不能为空")
        record = dict(self.admins.get(user_openid, {}))
        if union_openid:
            record["union-openid"] = union_openid
        elif "union-openid" not in record:
            record["union-openid"] = None
        if name:
            record["name"] = name
        else:
            record.setdefault("name", None)
        if record.get("added-at") is None:
            record["added-at"] = time.time() if now is None else now
        record.setdefault("group-identities", {})
        self.admins[user_openid] = record
        self._save()

    def bind_admin_group(
        self, user_openid: str, group_openid: str, member_openid: str,
    ) -> None:
        if user_openid not in self.admins:
            raise TargetStateError("要绑定的管理员不存在")
        if not group_openid or not member_openid:
            raise TargetStateError("群聊管理员身份不能为空")
        record = self.admins[user_openid]
        identities = dict(record.get("group-identities", {}))
        identities[group_openid] = member_openid
        record["group-identities"] = identities
        self._save()

    def reset(self) -> None:
        """清空 QQ 授权状态，由维护入口在主服务停止时调用。"""
        self.admins.clear()
        self.users.clear()
        self.groups.clear()
        self.pending.clear()
        self._save()

    @classmethod
    def reset_file(cls, path: Path) -> None:
        """不解析旧内容，直接以当前空状态原子覆盖目标文件。"""
        store = cls()
        store.path = path
        store._save()

    def admin_id(
        self, sender_id: str, union_openid: str | None = None, *,
        scene: str | None = None, group_openid: str | None = None,
    ) -> str | None:
        if union_openid:
            for user_openid, record in self.admins.items():
                if record.get("union-openid") == union_openid:
                    return user_openid
        if scene == "group":
            for user_openid, record in self.admins.items():
                identities = record.get("group-identities", {})
                if group_openid and identities.get(group_openid) == sender_id:
                    return user_openid
        if scene in (None, "user") and sender_id in self.admins:
            return sender_id
        return None

    def is_admin(
        self, sender_id: str, union_openid: str | None = None, *,
        scene: str | None = None, group_openid: str | None = None,
    ) -> bool:
        return self.admin_id(
            sender_id, union_openid, scene=scene, group_openid=group_openid,
        ) is not None

    def is_user(self, user_openid: str) -> bool:
        return user_openid in self.users

    def is_group(self, group_openid: str) -> bool:
        return group_openid in self.groups

    def is_target(self, kind: str, identifier: str) -> bool:
        if kind == "user":
            return identifier in self.admins or identifier in self.users
        if kind == "group":
            return identifier in self.groups
        return False

    def targets(self) -> frozenset[Target]:
        return frozenset(
            [Target("user", identifier) for identifier in self.admins]
            + [Target("user", identifier) for identifier in self.users]
            + [Target("group", identifier) for identifier in self.groups]
        )

    def private_targets(self) -> frozenset[Target]:
        """异常告警发送给管理员和已批准的单聊订阅者。"""
        return frozenset(
            Target("user", identifier)
            for identifier in (*self.admins, *self.users)
        )

    def group_targets(self) -> frozenset[Target]:
        """公开状态通知只发送给已批准群聊。"""
        return frozenset(Target("group", identifier) for identifier in self.groups)

    def request(
        self, kind: str, identifier: str, name: str, requester: str, *,
        requester_id: str, message_id: str,
        union_openid: str | None = None, now: float | None = None,
    ) -> tuple[SubscriptionRequest | None, bool]:
        if kind not in ("user", "group"):
            raise TargetStateError("订阅类型必须是 user 或 group")
        if not identifier or not name or not requester:
            raise TargetStateError("订阅申请缺少必要字段")
        current = time.time() if now is None else now
        self._expire(current)
        if self.is_target(kind, identifier):
            return None, False
        for request_id, item in self.pending.items():
            if item.get("kind") == kind and item.get("identifier") == identifier:
                request = self._request(request_id, item)
                last_notified = float(item.get("last-notified-at", request.created_at))
                notify = current - last_notified >= REQUEST_REMIND
                if notify:
                    item["last-notified-at"] = current
                    self._save()
                return request, notify
        request_id = secrets.token_hex(3)
        item = {
            "purpose": "subscription",
            "kind": kind,
            "identifier": identifier,
            "name": name,
            "requester": requester,
            "requester-id": requester_id,
            "message-id": message_id,
            "union-openid": union_openid,
            "created-at": current,
            "last-notified-at": current,
        }
        self.pending[request_id] = item
        self._save()
        return self._request(request_id, item), True

    def request_management(
        self, group_openid: str, member_openid: str, requester: str, *,
        message_id: str, now: float | None = None,
    ) -> tuple[SubscriptionRequest, bool]:
        current = time.time() if now is None else now
        self._expire(current)
        for request_id, item in self.pending.items():
            if (
                item.get("purpose") == "management"
                and item.get("identifier") == group_openid
                and item.get("requester-id") == member_openid
            ):
                request = self._request(request_id, item)
                last = float(item.get("last-notified-at", request.created_at))
                notify = current - last >= REQUEST_REMIND
                if notify:
                    item["last-notified-at"] = current
                    self._save()
                return request, notify
        request_id = secrets.token_hex(3)
        item = {
            "purpose": "management", "kind": "group",
            "identifier": group_openid, "name": f"群 …{group_openid[-6:]}",
            "requester": requester, "requester-id": member_openid,
            "message-id": message_id, "union-openid": None,
            "created-at": current, "last-notified-at": current,
        }
        self.pending[request_id] = item
        self._save()
        return self._request(request_id, item), True

    def decide(
        self, request_id: str, approved: bool, *,
        approved_by: str | None = None, now: float | None = None,
    ) -> SubscriptionRequest:
        current = time.time() if now is None else now
        self._expire(current)
        item = self.pending.pop(request_id, None)
        if item is None:
            raise TargetStateError("申请不存在或已经处理过啦")
        request = self._request(request_id, item)
        if approved:
            if request.purpose == "management":
                self.bind_admin_group(
                    approved_by or "", request.identifier, request.requester_id,
                )
                self.pending.pop(request_id, None)
                self._save()
                return request
            record = {
                "name": request.name,
                "approved-at": current,
                "request-id": request.request_id,
            }
            if approved_by:
                record["approved-by"] = approved_by
            union_openid = item.get("union-openid")
            if union_openid:
                record["union-openid"] = union_openid
            if request.kind == "user":
                self.users[request.identifier] = record
            else:
                self.groups[request.identifier] = record
        self._save()
        return request

    def _expire(self, now: float) -> None:
        expired = [
            request_id for request_id, item in self.pending.items()
            if now - float(item.get("created-at", 0)) >= REQUEST_TTL
        ]
        if expired:
            for request_id in expired:
                self.pending.pop(request_id, None)
            self._save()

    @staticmethod
    def _request(request_id: str, item: dict) -> SubscriptionRequest:
        try:
            return SubscriptionRequest(
                request_id,
                str(item["kind"]),
                str(item["identifier"]),
                str(item["name"]),
                str(item["requester"]),
                str(item["requester-id"]),
                str(item["message-id"]),
                float(item["created-at"]),
                str(item.get("purpose", "subscription")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TargetStateError("QQ 待审批申请格式无效") from exc
