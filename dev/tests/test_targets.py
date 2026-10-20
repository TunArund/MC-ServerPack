from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from monitor.src.network import Target
from monitor.src.targets import (
    REQUEST_REMIND, REQUEST_TTL, SubscriptionRequest, TargetStateError,
    TargetStore,
)


class TargetStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "qqbot.json"

    def request(self, store: TargetStore, **overrides):
        values = {
            "kind": "group",
            "identifier": "group-a",
            "name": "小伙伴群",
            "requester": "Alice",
            "requester_id": "member-a",
            "message_id": "message-a",
        }
        values.update(overrides)
        return store.request(**values)

    def test_admin_stores_delivery_and_union_identity(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a", "union-a", name="Alice", now=100)
        reloaded = TargetStore(self.path)
        self.assertTrue(reloaded.is_admin("user-a", scene="user"))
        self.assertTrue(reloaded.is_admin("member-x", "union-a", scene="group"))
        self.assertEqual(
            reloaded.admin_id("member-x", "union-a", scene="group"), "user-a",
        )
        self.assertEqual(reloaded.targets(), frozenset({Target("user", "user-a")}))

    def test_group_scene_without_union_never_matches_user_openid(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a")
        self.assertFalse(store.is_admin("user-a", scene="group"))
        self.assertFalse(store.is_admin("member-a", scene="group"))
        self.assertTrue(store.is_admin("user-a", scene="user"))

    def test_admin_group_identity_is_explicit_and_group_scoped(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a")
        store.bind_admin_group("user-a", "group-a", "member-a")
        reloaded = TargetStore(self.path)
        self.assertTrue(reloaded.is_admin(
            "member-a", scene="group", group_openid="group-a",
        ))
        self.assertFalse(reloaded.is_admin(
            "member-a", scene="group", group_openid="group-b",
        ))

    def test_management_request_approval_binds_requester(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("admin-a")
        request, notify = store.request_management(
            "group-a", "member-a", "Alice", message_id="m1", now=100,
        )
        self.assertTrue(notify)
        self.assertEqual(request.purpose, "management")
        store.decide(request.request_id, True, approved_by="admin-a", now=101)
        self.assertTrue(store.is_admin(
            "member-a", scene="group", group_openid="group-a",
        ))

    def test_re_adding_admin_keeps_existing_union_alias(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a", "union-a")
        store.add_admin("user-a")
        self.assertTrue(store.is_admin("member-x", "union-a", scene="group"))

    def test_state_has_no_version_and_is_saved_atomically(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a", "union-a")
        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("version", document)
        self.assertEqual(document["admins"][0]["user-openid"], "user-a")
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_old_list_format_is_rejected(self) -> None:
        self.path.write_text(json.dumps({
            "admins": ["user-a"], "groups": {}, "pending": {},
        }), encoding="utf-8")
        with self.assertRaisesRegex(TargetStateError, "旧版"):
            TargetStore(self.path)

    def test_user_request_is_approved_and_persisted(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("admin-a")
        request, notify = store.request(
            "user", "user-b", "Bob", "Bob",
            requester_id="user-b", message_id="message-b",
        )
        self.assertTrue(notify)
        self.assertIsInstance(request, SubscriptionRequest)
        self.assertEqual(request.kind, "user")
        approved = store.decide(
            request.request_id, True, approved_by="admin-a", now=200,
        )
        self.assertEqual(approved.identifier, "user-b")
        self.assertTrue(store.is_user("user-b"))
        self.assertTrue(store.is_target("user", "user-b"))
        reloaded = TargetStore(self.path)
        self.assertTrue(reloaded.is_user("user-b"))
        self.assertIn(Target("user", "user-b"), reloaded.targets())
        self.assertFalse(reloaded.pending)
        self.assertEqual(
            reloaded.users["user-b"]["request-id"], request.request_id,
        )

    def test_group_request_approval_and_rejection(self) -> None:
        store = TargetStore(self.path)
        request, _ = self.request(store)
        approved = store.decide(request.request_id, True, now=200)
        self.assertEqual(approved.identifier, "group-a")
        self.assertTrue(store.is_group("group-a"))
        self.assertIn(Target("group", "group-a"), store.targets())

        rejected, _ = self.request(store, identifier="group-b")
        store.decide(rejected.request_id, False, now=201)
        self.assertFalse(store.is_group("group-b"))
        self.assertNotIn(Target("group", "group-b"), store.targets())

    def test_duplicate_request_only_reminds_once_per_window(self) -> None:
        store = TargetStore(self.path)
        request, _ = self.request(store, now=100)
        same, notify = self.request(store, now=100 + REQUEST_REMIND)
        self.assertEqual(same.request_id, request.request_id)
        self.assertTrue(notify)
        _, notify_again = self.request(store, now=101 + REQUEST_REMIND)
        self.assertFalse(notify_again)

    def test_expired_request_cannot_be_approved(self) -> None:
        store = TargetStore(self.path)
        request, _ = self.request(store, now=100)
        with self.assertRaisesRegex(TargetStateError, "不存在"):
            store.decide(request.request_id, True, now=100 + REQUEST_TTL)

    def test_existing_target_cannot_request_again(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a")
        request, notify = store.request(
            "user", "user-a", "Alice", "Alice",
            requester_id="user-a", message_id="message-a",
        )
        self.assertIsNone(request)
        self.assertFalse(notify)
        store.groups["group-a"] = {"name": "群"}
        request, notify = self.request(store)
        self.assertIsNone(request)
        self.assertFalse(notify)

    def test_targets_include_admins_users_and_groups_without_duplicates(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("user-a")
        store.users["user-a"] = {"name": "Alice"}
        store.users["user-b"] = {"name": "Bob"}
        store.groups["group-a"] = {"name": "群"}
        self.assertEqual(
            store.targets(),
            frozenset({
                Target("user", "user-a"), Target("user", "user-b"),
                Target("group", "group-a"),
            }),
        )
        self.assertEqual(
            store.private_targets(),
            frozenset({Target("user", "user-a"), Target("user", "user-b")}),
        )
        self.assertEqual(
            store.group_targets(), frozenset({Target("group", "group-a")}),
        )

    def test_reset_clears_all_authorization_state(self) -> None:
        store = TargetStore(self.path)
        store.add_admin("admin-a")
        store.users["user-a"] = {"name": "用户"}
        store.groups["group-a"] = {"name": "群"}
        self.request(store, identifier="group-b")
        store.reset()
        reloaded = TargetStore(self.path)
        self.assertEqual(reloaded.admins, {})
        self.assertEqual(reloaded.users, {})
        self.assertEqual(reloaded.groups, {})
        self.assertEqual(reloaded.pending, {})

    def test_reset_file_can_replace_incompatible_old_state(self) -> None:
        self.path.write_text('{"admins":["old"]}\n', encoding="utf-8")
        TargetStore.reset_file(self.path)
        self.assertEqual(TargetStore(self.path).admins, {})


if __name__ == "__main__":
    unittest.main()
