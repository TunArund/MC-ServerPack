from __future__ import annotations

import unittest
from types import SimpleNamespace

from monitor.src.qqbot.message import menu, menu_keyboard
from monitor.src.qqbot.qqbot import QQBot
from monitor.src.qqbot.targets import Target, TargetPolicy, incoming


class FakeNetwork:
    def __init__(self) -> None:
        self.replies: list[tuple[object, object, int]] = []

    async def reply(self, received, message, *, sequence: int = 1) -> None:
        self.replies.append((received, message, sequence))


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


def group_event(message_id: str, sender: str, content: str) -> dict:
    return {
        "id": message_id,
        "group_openid": "group-a",
        "author": {"member_openid": sender},
        "content": content,
    }


class MessageTests(unittest.TestCase):
    def test_menu_is_markdown_with_only_requested_buttons(self) -> None:
        result = menu("RUNNING")
        labels = [
            button["render_data"]["label"]
            for row in result.keyboard["content"]["rows"]
            for button in row["buttons"]
        ]
        self.assertTrue(result.markdown)
        self.assertIn("RUNNING", result.content)
        self.assertEqual(labels, ["开机", "关机", "确认关机"])
        self.assertEqual(
            [button["action"]["data"] for button in menu_keyboard()["content"]["rows"][0]["buttons"]],
            labels,
        )


class TargetTests(unittest.TestCase):
    def test_group_event_is_normalized_and_mention_is_removed(self) -> None:
        result = incoming(
            "GROUP_AT_MESSAGE_CREATE",
            group_event("message-a", "user-a", "<@!bot> 菜单"),
        )
        self.assertEqual(result.target, Target("group", "group-a"))
        self.assertEqual(result.sender_id, "user-a")
        self.assertEqual(result.content, "菜单")


class QQBotTests(unittest.IsolatedAsyncioTestCase):
    def make_bot(self, *, clock=lambda: 100.0):
        network = FakeNetwork()
        power = FakePower()
        policy = TargetPolicy((Target("group", "group-a"),), ("admin-a",))
        return QQBot(network, policy, power, clock=clock), network, power

    async def test_menu_queries_status_and_returns_keyboard(self) -> None:
        bot, network, power = self.make_bot()
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m1", "user-a", "菜单"))
        self.assertEqual(power.calls, ["status"])
        self.assertTrue(network.replies[0][1].markdown)

    async def test_non_admin_cannot_start(self) -> None:
        bot, network, power = self.make_bot()
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m1", "user-a", "开机"))
        self.assertEqual(power.calls, [])
        self.assertIn("管理员", network.replies[0][1].content)

    async def test_stop_requires_same_admin_confirmation(self) -> None:
        now = [100.0]
        bot, network, power = self.make_bot(clock=lambda: now[0])
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m1", "admin-a", "关机"))
        self.assertEqual(power.calls, [])
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m2", "admin-a", "确认关机"))
        self.assertEqual(power.calls, ["stop"])
        self.assertEqual(network.replies[-1][2], 2)

    async def test_expired_confirmation_does_not_stop(self) -> None:
        now = [100.0]
        bot, network, power = self.make_bot(clock=lambda: now[0])
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m1", "admin-a", "关机"))
        now[0] = 161.0
        await bot.handle("GROUP_AT_MESSAGE_CREATE", group_event("m2", "admin-a", "确认关机"))
        self.assertEqual(power.calls, [])
        self.assertIn("没有待确认", network.replies[-1][1].content)


if __name__ == "__main__":
    unittest.main()
