from __future__ import annotations

import unittest
from types import SimpleNamespace

from monitor.src.qqbot.message import render
from monitor.src.qqbot.network import Network, Outgoing
from monitor.src.qqbot.qqbot import QQBot, menu, menu_keyboard
from monitor.src.qqbot.targets import Target, TargetPolicy, incoming


class FakeNetwork:
    def __init__(self) -> None:
        self.replies: list[tuple[object, object, int]] = []
        self.pushes: list[tuple[object, object]] = []

    async def reply(self, received, message, *, sequence: int = 1) -> None:
        self.replies.append((received, message, sequence))

    async def push(self, target, message) -> None:
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


def group_event(message_id: str, sender: str, content: str) -> dict:
    return {
        "id": message_id,
        "group_openid": "group-a",
        "author": {"member_openid": sender},
        "content": content,
    }


class MessageTests(unittest.TestCase):
    def test_external_template_is_rendered(self) -> None:
        self.assertEqual(render("done", action="开机", state="RUNNING"),
                         "开机请求已提交，当前状态：RUNNING")

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

    def test_active_message_has_no_reply_fields(self) -> None:
        body = Network._body(Outgoing("告警"))
        self.assertEqual(body, {"msg_type": 0, "content": "告警"})
        reply = Network._body(Outgoing("回复"), message_id="m1", sequence=2)
        self.assertEqual(reply["msg_id"], "m1")
        self.assertEqual(reply["msg_seq"], 2)

    def test_direct_message_uses_user_endpoint(self) -> None:
        self.assertTrue(
            Network._endpoint(Target("user", "owner")).endswith(
                "/v2/users/owner/messages",
            ),
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
    def make_bot(self, *, clock=lambda: 100.0, alert_window: float = 0):
        network = FakeNetwork()
        power = FakePower()
        group = Target("group", "group-a")
        policy = TargetPolicy(
            (group,), ("admin-a",),
            {
                "check-failed": frozenset((group,)),
                "recovered": frozenset((group,)),
            },
        )
        return QQBot(
            network, policy, power, clock=clock, alert_window=alert_window,
        ), network, power

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

    async def test_alerts_are_aggregated_and_pushed_to_each_target(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.alert("check-failed", "电源状态异常")
        await bot.alert("check-failed", "电源状态异常")
        await bot.alert("check-failed", "API 暂时不可用")
        await bot.flush_alerts()
        self.assertEqual(len(network.pushes), 1)
        target, outgoing = network.pushes[0]
        self.assertEqual(target, Target("group", "group-a"))
        self.assertIn("电源状态异常（2 次）", outgoing.content)
        self.assertIn("API 暂时不可用", outgoing.content)

    async def test_recovery_uses_external_template(self) -> None:
        bot, network, _ = self.make_bot()
        await bot.alert("recovered", "")
        await bot.flush_alerts()
        self.assertIn("恢复正常", network.pushes[0][1].content)

    async def test_each_alert_kind_uses_only_its_configured_targets(self) -> None:
        network = FakeNetwork()
        owner = Target("user", "owner")
        players = Target("group", "players")
        policy = TargetPolicy(
            (owner, players), ("owner",),
            {
                "check-failed": frozenset((owner,)),
                "state-mismatch": frozenset((players,)),
            },
        )
        bot = QQBot(network, policy, FakePower(), alert_window=0)
        await bot.alert("check-failed", "API 失败")
        await bot.alert("state-mismatch", "状态不符")
        await bot.flush_alerts()
        sent = {target: outgoing.content for target, outgoing in network.pushes}
        self.assertIn("API 失败", sent[owner])
        self.assertNotIn("状态不符", sent[owner])
        self.assertIn("状态不符", sent[players])
        self.assertNotIn("API 失败", sent[players])

    async def test_target_without_alert_route_is_not_notified(self) -> None:
        network = FakeNetwork()
        target = Target("group", "players")
        bot = QQBot(
            network, TargetPolicy((target,), (), {}), FakePower(), alert_window=0,
        )
        await bot.alert("check-failed", "API 失败")
        await bot.flush_alerts()
        self.assertEqual(network.pushes, [])


if __name__ == "__main__":
    unittest.main()
