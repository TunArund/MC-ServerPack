from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from monitor.src.message import MessageError, MessageRenderer, messages
from monitor.src.manager_sdk import PublicModpack
from monitor.src.network import Network, Outgoing, Target, incoming
from monitor.src.qqbot import QQBot, discover_target
from monitor.src.targets import TargetStore


class FakeNetwork:
    def __init__(self) -> None:
        self.replies: list[tuple[object, object, int]] = []
        self.reply_to_calls: list[tuple[Target, str, object, int]] = []
        self.pushes: list[tuple[Target, object]] = []
        self.reply_to_error: Exception | None = None
    async def reply(self, received, message, *, sequence: int = 1) -> None:
        self.replies.append((received, message, sequence))

    async def reply_to(
        self, target: Target, message_id: str, message, *, sequence: int = 1,
    ) -> None:
        self.reply_to_calls.append((target, message_id, message, sequence))
        if self.reply_to_error is not None:
            raise self.reply_to_error

    async def push(self, target: Target, message) -> None:
        self.pushes.append((target, message))


class FakePower:
    def __init__(self, state: str = "STOPPED") -> None:
        self.state = state
        self.calls: list[str] = []

    async def status(self):
        self.calls.append("status")
        return SimpleNamespace(state=self.state)

    async def start(self):
        self.calls.append("start")
        self.state = "STARTING"
        return SimpleNamespace(state=self.state)

    async def stop(self):
        self.calls.append("stop")
        self.state = "STOPPING"
        return SimpleNamespace(state=self.state)


class FakeManagerStatus:
    def __init__(self, modpacks) -> None:
        self.modpacks = tuple(modpacks)
        self.hosts: list[str] = []

    def list(self, host: str):
        self.hosts.append(host)
        return self.modpacks


def group_event(
    message_id: str, sender: str, content: str, role: str = "member",
    union: str | None = None,
) -> dict:
    author: dict = {"member_openid": sender, "member_role": role}
    if union:
        author["union_openid"] = union
    return {
        "id": message_id,
        "group_openid": "group-a",
        "author": author,
        "content": content,
    }


def user_event(
    message_id: str, sender: str, content: str, union: str | None = None,
) -> dict:
    author: dict = {"user_openid": sender}
    if union:
        author["union_openid"] = union
    return {
        "id": message_id,
        "author": author,
        "content": content,
    }


def make_store(
    *, admins=(("admin-a", None),), groups=(), users=(),
) -> TargetStore:
    store = TargetStore()
    for user_openid, union_openid in admins:
        if user_openid is not None:
            store.add_admin(user_openid, union_openid)
    for group_openid in groups:
        store.groups[group_openid] = {"name": group_openid}
    for user_openid in users:
        store.users[user_openid] = {"name": user_openid}
    return store


class MessageTests(unittest.TestCase):
    def test_external_template_is_rendered(self) -> None:
        result = messages.render("done", action="开机", state="RUNNING")
        self.assertIn("请求已提交", result.content)
        self.assertIn("RUNNING", result.content)

    def test_menu_is_markdown_with_power_buttons(self) -> None:
        result = messages.render("menu", state="RUNNING")
        labels = [
            button["render_data"]["label"]
            for row in result.keyboard["content"]["rows"]
            for button in row["buttons"]
        ]
        self.assertTrue(result.markdown)
        self.assertIn("RUNNING", result.content)
        self.assertEqual(labels, ["开机", "关机", "取消预订", "恢复预订"])

    def test_readonly_menu_has_no_keyboard(self) -> None:
        result = messages.render("menu-readonly", state="STOPPED")
        self.assertFalse(result.markdown)
        self.assertIn("管理员", result.content)
        self.assertIsNone(result.keyboard)

    def test_alert_keyboard_offers_fixed_snooze_durations(self) -> None:
        result = messages.render("alert-state-mismatch-stop", alerts="- 状态异常")
        labels = [
            button["action"]["data"]
            for button in result.keyboard["content"]["rows"][0]["buttons"]
        ]
        self.assertEqual(labels, ["关机", "1h", "2h", "5h"])

    def test_recovery_message_does_not_repeat_stale_failure_details(self) -> None:
        result = messages.render("alert-recovered", alerts="实际状态：RUNNING")
        self.assertIn("状态异常已经解除", result.content)
        self.assertNotIn("RUNNING", result.content)

    def test_request_message_carries_dynamic_decision_data(self) -> None:
        result = messages.render(
            "subscription-request", kind_label="群聊", name_label="群名称",
            name="群", requester="Alice", request_id="a1b2c3",
            button_data={
                "request-approve": "批准 a1b2c3",
                "request-reject": "拒绝 a1b2c3",
            },
        )
        buttons = result.keyboard["content"]["rows"][0]["buttons"]
        self.assertEqual(
            [button["action"]["data"] for button in buttons],
            ["批准 a1b2c3", "拒绝 a1b2c3"],
        )

    def test_template_hot_reloads_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.yml"
            path.write_text(
                "keyboard:\n  menu: 菜单\nmenu:\n  text: 旧文案\n",
                encoding="utf-8",
            )
            renderer = MessageRenderer(path)
            self.assertEqual(renderer.render("menu").content, "旧文案")
            path.write_text(
                "keyboard:\n  menu: 菜单\nmenu:\n  text: 新文案\n",
                encoding="utf-8",
            )
            newer = time.time_ns() + 1_000_000
            os.utime(path, ns=(newer, newer))
            self.assertEqual(renderer.render("menu").content, "新文案")

    def test_broken_hot_reload_keeps_previous_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.yml"
            path.write_text(
                "keyboard:\n  menu: 菜单\nmenu:\n  text: 旧文案\n",
                encoding="utf-8",
            )
            renderer = MessageRenderer(path)
            path.write_text("menu: [\n", encoding="utf-8")
            newer = time.time_ns() + 1_000_000
            os.utime(path, ns=(newer, newer))
            self.assertEqual(renderer.render("menu").content, "旧文案")

    def test_keyboard_services_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.yml"
            path.write_text(
                "keyboard:\n  unknown: 未知\nmenu:\n  text: 文案\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MessageError, "未实现服务"):
                MessageRenderer(path, services={"menu"})

    def test_layout_can_hide_button_without_changing_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.yml"
            path.write_text(
                "keyboard:\n  power-stop: 关机\n"
                "menu:\n  markdown: 你想做什么？\n  keyboard:\n    - [power-stop]\n",
                encoding="utf-8",
            )
            rendered = MessageRenderer(path).render("menu")
        buttons = rendered.keyboard["content"]["rows"][0]["buttons"]
        self.assertEqual([button["id"] for button in buttons], ["power-stop"])

    def test_plain_text_template_cannot_silently_attach_keyboard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "message.yml"
            path.write_text(
                "keyboard:\n  power-stop: 关机\n"
                "menu:\n  text: 你想做什么？\n  keyboard:\n    - [power-stop]\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(MessageError, "含按钮时必须使用 markdown"):
                MessageRenderer(path)

    def test_active_message_has_no_reply_fields(self) -> None:
        body = Network._body(Outgoing("告警"))
        self.assertEqual(body, {"msg_type": 0, "content": "告警"})
        reply = Network._body(Outgoing("回复"), message_id="m1", sequence=2)
        self.assertEqual(reply["msg_id"], "m1")
        self.assertEqual(reply["msg_seq"], 2)

    def test_markdown_keyboard_omits_empty_content_field(self) -> None:
        message = Outgoing(
            "🎮 游戏服务器当前状态：STOPPED",
            markdown=True,
            keyboard={"content": {"rows": []}},
        )
        body = Network._body(message, message_id="m1")
        self.assertEqual(body["msg_type"], 2)
        self.assertIn("keyboard", body)
        self.assertTrue(body["markdown"]["content"].startswith("🎮"))
        self.assertNotIn("content", body)

    def test_markdown_message_omits_content(self) -> None:
        body = Network._body(Outgoing("带按钮的正文", markdown=True))
        self.assertEqual(body, {
            "msg_type": 2,
            "markdown": {"content": "带按钮的正文"},
        })

    def test_endpoints_use_scene_delivery_ids(self) -> None:
        self.assertTrue(
            Network._endpoint(Target("user", "owner")).endswith(
                "/v2/users/owner/messages",
            ),
        )
        self.assertTrue(
            Network._endpoint(Target("group", "group-a")).endswith(
                "/v2/groups/group-a/messages",
            ),
        )


class IncomingTests(unittest.TestCase):
    def test_group_event_keeps_scene_ids_and_parses_union(self) -> None:
        result = incoming(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "<@!bot> 菜单", union="union-a"),
        )
        self.assertEqual(result.target, Target("group", "group-a"))
        self.assertEqual(result.sender_id, "member-a")
        self.assertEqual(result.union_openid, "union-a")
        self.assertEqual(result.content, "菜单")

    def test_c2c_event_keeps_user_openid_and_parses_union(self) -> None:
        result = incoming(
            "C2C_MESSAGE_CREATE",
            user_event("m1", "user-a", "菜单", union="union-a"),
        )
        self.assertEqual(result.target, Target("user", "user-a"))
        self.assertEqual(result.sender_id, "user-a")
        self.assertEqual(result.union_openid, "union-a")

    def test_empty_union_becomes_none(self) -> None:
        result = incoming(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-a", "菜单", union=""),
        )
        self.assertIsNone(result.union_openid)


class QQBotTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, store=None, clock=lambda: 100.0, alert_window=0.0):
        network = FakeNetwork()
        power = FakePower()
        bot = QQBot(
            network, power, clock=clock, alert_window=alert_window,
            targets=store if store is not None else make_store(),
        )
        return bot, network, power

    def pending(self, network: FakeNetwork) -> str:
        for _, message in network.pushes:
            for row in (message.keyboard or {}).get("content", {}).get("rows", []):
                for button in row["buttons"]:
                    data = button["action"]["data"]
                    if data.startswith("批准"):
                        return data.split(" ", 1)[1]
        raise AssertionError("没有找到审批按钮")

    async def test_unapproved_group_member_gets_no_reply(self) -> None:
        bot, network, power = self.make_bot(store=make_store(admins=(), groups=()))
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE", group_event("m1", "member-a", "菜单"),
        )
        self.assertEqual(network.replies, [])
        self.assertEqual(power.calls, [])

    async def test_public_started_uses_manager_list_and_only_notifies_groups(self) -> None:
        store = make_store(groups=("group-a",), users=("user-b",))
        manager = FakeManagerStatus((
            PublicModpack(
                "closing-song", "落幕曲", "running", 27015,
                "mc.example.com:27015",
            ),
            PublicModpack(
                "vanilla", "原版", "stopped", 25565,
                "mc.example.com:25565",
            ),
        ))
        network = FakeNetwork()
        bot = QQBot(
            network, FakePower(), alert_window=0, targets=store,
            manager_status=manager,
        )
        async def direct(call, *args):
            return call(*args)

        with patch("monitor.src.qqbot.asyncio.to_thread", side_effect=direct):
            await bot.notify("running", "203.0.113.10")
        self.assertEqual(manager.hosts, ["203.0.113.10"])
        self.assertEqual([target for target, _ in network.pushes], [Target("group", "group-a")])
        self.assertIn("落幕曲", network.pushes[0][1].content)
        self.assertNotIn("原版", network.pushes[0][1].content)

    async def test_public_stopped_only_notifies_groups_without_manager_api(self) -> None:
        store = make_store(groups=("group-a",), users=("user-b",))
        network = FakeNetwork()
        bot = QQBot(network, FakePower(), alert_window=0, targets=store)
        await bot.notify("stopped")
        self.assertEqual([target for target, _ in network.pushes], [Target("group", "group-a")])
        self.assertIn("服务器已关机", network.pushes[0][1].content)

    async def test_admin_menu_in_private_has_power_buttons(self) -> None:
        bot, network, power = self.make_bot()
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "菜单"))
        self.assertEqual(power.calls, ["status"])
        self.assertTrue(network.replies[0][1].markdown)
        self.assertIsNotNone(network.replies[0][1].keyboard)

    async def test_approved_user_menu_is_read_only(self) -> None:
        store = make_store(users=("user-b",))
        bot, network, power = self.make_bot(store=store)
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "user-b", "菜单"))
        self.assertEqual(power.calls, ["status"])
        self.assertIsNone(network.replies[0][1].keyboard)
        self.assertIn("管理员", network.replies[0][1].content)

    async def test_approved_group_menu_is_read_only(self) -> None:
        store = make_store(groups=("group-a",))
        bot, network, _ = self.make_bot(store=store)
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE", group_event("m1", "member-a", "菜单"),
        )
        self.assertIsNone(network.replies[0][1].keyboard)

    async def test_non_admin_cannot_operate(self) -> None:
        store = make_store(users=("user-b",))
        bot, network, power = self.make_bot(store=store)
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "user-b", "开机"))
        self.assertEqual(power.calls, [])
        self.assertIn("管理员", network.replies[0][1].content)

    async def test_admin_operates_from_private_chat(self) -> None:
        bot, network, power = self.make_bot()
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "开机"))
        self.assertEqual(power.calls, ["start"])
        self.assertIn("开机请求", network.replies[0][1].content)
        self.assertIn("请求已提交", network.replies[1][1].content)

    async def test_admin_operates_from_group_chat_with_union(self) -> None:
        store = make_store(admins=(("admin-a", "union-a"),))
        bot, network, power = self.make_bot(store=store)
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "开机", union="union-a"),
        )
        self.assertEqual(power.calls, ["start"])
        self.assertIn("开机请求", network.replies[0][1].content)

    async def test_group_admin_without_union_is_denied(self) -> None:
        store = make_store(
            admins=(("admin-a", None),), groups=("group-a",),
        )
        bot, network, power = self.make_bot(store=store)
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE", group_event("m1", "admin-a", "开机"),
        )
        self.assertEqual(power.calls, [])
        self.assertIn("管理员", network.replies[0][1].content)

    async def test_stop_requires_confirmation_in_same_scene(self) -> None:
        now = [100.0]
        bot, network, power = self.make_bot(clock=lambda: now[0])
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "关机"))
        self.assertEqual(power.calls, [])
        self.assertIn("确认关机", network.replies[0][1].content)
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m2", "admin-a", "确认关机"),
        )
        self.assertEqual(power.calls, ["stop"])
        self.assertEqual(network.replies[-1][2], 2)

    async def test_expired_confirmation_does_not_stop(self) -> None:
        now = [100.0]
        bot, network, power = self.make_bot(clock=lambda: now[0])
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "关机"))
        now[0] = 161.0
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m2", "admin-a", "确认关机"),
        )
        self.assertEqual(power.calls, [])
        self.assertIn("超过 60 秒", network.replies[-1][1].content)

    async def test_request_without_admin_is_rejected(self) -> None:
        bot, network, _ = self.make_bot(store=make_store(admins=(), groups=()))
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-b", "订阅 测试"),
        )
        self.assertIn("尚未设置管理员", network.replies[0][1].content)
        self.assertEqual(bot.targets.pending, {})
        self.assertEqual(network.pushes, [])

    async def test_c2c_request_notifies_all_admins(self) -> None:
        store = make_store(admins=(("admin-a", None), ("admin-b", None)))
        bot, network, _ = self.make_bot(store=store)
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-b", "订阅 测试"),
        )
        self.assertIn("订阅申请已提交", network.replies[0][1].content)
        self.assertEqual(
            {target.identifier for target, _ in network.pushes},
            {"admin-a", "admin-b"},
        )
        request_id = self.pending(network)
        self.assertEqual(len(request_id), 6)
        self.assertEqual(len(bot.targets.pending), 1)

    async def test_group_request_notifies_all_admins(self) -> None:
        store = make_store(admins=(("admin-a", None),))
        bot, network, _ = self.make_bot(store=store)
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "订阅 小伙伴群", union="union-b"),
        )
        self.assertIn("订阅申请已提交", network.replies[0][1].content)
        self.assertEqual(network.pushes[0][0], Target("user", "admin-a"))
        self.assertIn("小伙伴群", network.pushes[0][1].content)
        self.assertEqual(
            bot.targets.pending[next(iter(bot.targets.pending))]["kind"], "group",
        )

    async def test_duplicate_request_inside_window_reports_pending(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-b", "订阅 测试"),
        )
        network.replies.clear()
        network.pushes.clear()
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m2", "user-b", "订阅 测试"),
        )
        self.assertIn("等待管理员审批", network.replies[0][1].content)
        self.assertEqual(network.pushes, [])

    async def test_admin_request_is_reported_as_existing(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "订阅 测试"),
        )
        self.assertIn("已经订阅", network.replies[0][1].content)
        self.assertEqual(bot.targets.pending, {})

    async def test_group_request_uses_provided_group_note(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "订阅 测试群"),
        )
        content = network.pushes[0][1].content
        self.assertIn("群备注", content)
        self.assertIn("测试群", content)

    async def test_group_request_falls_back_to_generated_name(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "订阅"),
        )
        content = network.pushes[0][1].content
        self.assertIn("群标识", content)
        self.assertIn("…", content)

    async def test_admin_can_request_an_unapproved_group(self) -> None:
        store = make_store(admins=(("admin-a", "union-a"),))
        bot, network, _ = self.make_bot(store=store)
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event(
                "m1", "member-a", "订阅 小伙伴群", union="union-a",
            ),
        )
        self.assertIn("订阅申请已提交", network.replies[0][1].content)
        self.assertEqual(network.pushes[0][0], Target("user", "admin-a"))
        self.assertEqual(len(bot.targets.pending), 1)

    async def test_any_admin_can_approve_user_request(self) -> None:
        store = make_store(admins=(("admin-a", None), ("admin-b", None)))
        bot, network, _ = self.make_bot(store=store)
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-b", "订阅 测试"),
        )
        request_id = self.pending(network)
        network.pushes.clear()
        network.replies.clear()
        await bot.handle(
            "C2C_MESSAGE_CREATE",
            user_event("m2", "admin-b", f"批准 {request_id}"),
        )
        self.assertTrue(bot.targets.is_user("user-b"))
        self.assertIn("批准", network.replies[0][1].content)
        self.assertEqual(
            network.reply_to_calls[0][0], Target("user", "user-b"),
        )
        self.assertIn("已批准订阅", network.reply_to_calls[0][2].content)

    async def test_non_admin_cannot_approve(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "C2C_MESSAGE_CREATE", user_event("m1", "user-b", "订阅 测试"),
        )
        request_id = self.pending(network)
        network.replies.clear()
        await bot.handle(
            "C2C_MESSAGE_CREATE",
            user_event("m2", "user-b", f"批准 {request_id}"),
        )
        self.assertFalse(bot.targets.is_user("user-b"))
        self.assertIn("管理员", network.replies[0][1].content)

    async def test_group_approval_notifies_group_with_original_message(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "订阅 小伙伴群"),
        )
        request_id = self.pending(network)
        network.pushes.clear()
        await bot.handle(
            "C2C_MESSAGE_CREATE",
            user_event("m2", "admin-a", f"批准 {request_id}"),
        )
        self.assertTrue(bot.targets.is_group("group-a"))
        target, message_id, message, _ = network.reply_to_calls[0]
        self.assertEqual(target, Target("group", "group-a"))
        self.assertEqual(message_id, "m1")
        self.assertNotIn("<@", message.content)

    async def test_rejection_falls_back_to_active_message(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("m1", "member-a", "订阅 小伙伴群"),
        )
        request_id = self.pending(network)
        network.pushes.clear()
        network.reply_to_error = RuntimeError("expired")
        await bot.handle(
            "C2C_MESSAGE_CREATE",
            user_event("m2", "admin-a", f"拒绝 {request_id}"),
        )
        self.assertFalse(bot.targets.is_group("group-a"))
        self.assertEqual(network.pushes[0][0], Target("group", "group-a"))
        self.assertIn("未批准", network.pushes[0][1].content)
        self.assertNotIn("<@", network.pushes[0][1].content)

    async def test_unknown_request_id_reports_error(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle(
            "C2C_MESSAGE_CREATE",
            user_event("m1", "admin-a", "批准 abcdef"),
        )
        self.assertIn("没法处理", network.replies[0][1].content)

    async def test_bare_decision_label_reports_usage(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "批准"))
        self.assertIn("批准 编号", network.replies[0][1].content)

    async def test_admin_can_cancel_restore_and_snooze(self) -> None:
        calls = []

        async def cancel():
            calls.append("cancel")
            return SimpleNamespace(due_at=SimpleNamespace(
                strftime=lambda _format: "10-18 20:00",
            ))

        async def snooze(hours: int):
            calls.append(hours)
            return SimpleNamespace(strftime=lambda _format: "10-18 18:00")

        async def restore():
            calls.append("restore")
            return SimpleNamespace(due_at=SimpleNamespace(
                strftime=lambda _format: "10-18 20:00",
            ))

        bot, network, _ = self.make_bot()
        bot.cancel_reservation = cancel
        bot.restore_reservation = restore
        bot.snooze_alerts = snooze
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m1", "admin-a", "取消预订"))
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m2", "admin-a", "恢复预订"))
        await bot.handle("C2C_MESSAGE_CREATE", user_event("m3", "admin-a", "2h"))
        self.assertEqual(calls, ["cancel", "restore", 2])
        self.assertIn("10-18 20:00", network.replies[0][1].content)
        self.assertIn("已恢复", network.replies[1][1].content)

    async def test_alerts_go_only_to_private_targets(self) -> None:
        store = make_store(
            admins=(("admin-a", None),),
            users=("user-b",),
            groups=("group-a",),
        )
        bot, network, _ = self.make_bot(store=store)
        await bot.alert("check-failed", "API 失败")
        await bot.alert("state-mismatch-stop", "状态不符")
        await bot.flush_alerts()
        targets = {target for target, _ in network.pushes}
        self.assertEqual(
            targets,
            {
                Target("user", "admin-a"), Target("user", "user-b"),
            },
        )
        self.assertEqual(len(network.pushes), 4)
        contents = [message.content for _, message in network.pushes]
        self.assertTrue(any("API 失败" in content for content in contents))
        self.assertTrue(any("状态不符" in content for content in contents))

    async def test_state_mismatch_alert_reports_only_latest_sample(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.alert("state-mismatch-stop", "实际状态：STARTING")
        await bot.alert("state-mismatch-stop", "实际状态：RUNNING")
        await bot.flush_alerts()
        content = network.pushes[0][1].content
        self.assertNotIn("STARTING", content)
        self.assertIn("RUNNING", content)


class DiscoveryNetwork(FakeNetwork):
    def __init__(self, events: list[tuple[str, dict]]) -> None:
        super().__init__()
        self.events = events

    async def serve(self, handler, stopped) -> None:
        for event_name, event in self.events:
            await handler(event_name, event)
            if stopped.is_set():
                return
        await stopped.wait()


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_discovery_requires_exact_c2c_code(self) -> None:
        network = DiscoveryNetwork([
            ("C2C_MESSAGE_CREATE", user_event("m1", "wrong-user", "0000")),
            ("GROUP_AT_MESSAGE_CREATE", group_event("m2", "group-user", "a1b2")),
            ("C2C_MESSAGE_CREATE", user_event(
                "m3", "owner-openid", "a1b2", union="union-owner",
            )),
        ])
        result = await discover_target(network, "a1b2", "user", timeout=1)
        self.assertEqual(result.sender_id, "owner-openid")
        self.assertEqual(result.union_openid, "union-owner")
        self.assertEqual(len(network.replies), 1)
        self.assertIn("验证成功", network.replies[0][1].content)

    async def test_group_discovery_returns_group_and_owner_identity(self) -> None:
        network = DiscoveryNetwork([
            ("GROUP_AT_MESSAGE_CREATE", group_event(
                "m1", "member-openid", "a1b2", "owner", union="union-owner",
            )),
        ])
        result = await discover_target(network, "a1b2", "group", timeout=1)
        self.assertEqual(result.target, Target("group", "group-a"))
        self.assertEqual(result.sender_id, "member-openid")
        self.assertEqual(result.sender_role, "owner")


if __name__ == "__main__":
    unittest.main()
