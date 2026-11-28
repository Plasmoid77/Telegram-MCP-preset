"""policy.py: what a run may call. Table-driven, no network."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
import policy  # noqa: E402

CHAT, NOTIFY = -100123, 100


class ChatScope(unittest.TestCase):
    def check(self, name, args, private=False):
        return policy.check("chat", name, args, CHAT, private)

    def test_current_chat_reads_and_sends_are_pinned(self):
        self.assertEqual(self.check("get_history", {"chat_id": str(CHAT), "limit": 20}), {"chat_id": CHAT, "limit": 20})
        self.assertEqual(self.check("send_message", {"chat_id": CHAT, "message": "hi", "reply_to": 5}),
                         {"chat_id": CHAT, "message": "hi", "reply_to": 5})

    def test_video_window_must_be_sane_seconds(self):
        ok = {"chat_id": CHAT, "message_id": 5, "start": 0, "end": 12.5, "max_frames": 99, "transcribe": False}
        self.assertEqual(self.check("view_video", ok), dict(ok, max_frames=16))
        self.assertEqual(self.check("view_video", {"chat_id": CHAT, "message_id": 5, "end": 3}),
                         {"chat_id": CHAT, "message_id": 5, "end": 3})
        for bad in ({"start": -1}, {"start": 5, "end": 5}, {"start": float("nan")}, {"end": float("inf")},
                    {"start": True}, {"start": "10"}, {"transcribe": "yes"}, {"transcribe": None},
                    {"end": 0}, {"start": 10 ** 400}, {"start": 3, "end": 2}):
            self.assertIsNone(self.check("view_video", dict({"chat_id": CHAT, "message_id": 5}, **bad)), bad)

    def test_other_chat_is_denied_everywhere(self):
        for name in policy.CHAT_TOOLS:
            with self.subTest(name):
                self.assertIsNone(self.check(name, {"chat_id": 555}))
                self.assertIsNone(self.check(name, {"chat_id": "@someone"}))
                self.assertIsNone(self.check(name, {}))

    def test_other_chat_via_notify_or_forward_is_denied(self):
        # chat-scope runs reach the owner only through the broker's notify_owner
        self.assertIsNone(self.check("send_message", {"chat_id": NOTIFY, "message": "x"}))
        self.assertIsNone(self.check("forward_message", {"from_chat_id": CHAT, "message_id": 1, "to_chat_id": NOTIFY}))

    def test_account_wide_tools_are_denied(self):
        for name in ("list_chats", "search_global", "resolve_username", "add_chat_to_folder", "leave_chat",
                     "update_profile", "export_chat_history", "send_file", "delete_message", "edit_message"):
            with self.subTest(name):
                self.assertIsNone(self.check(name, {"chat_id": CHAT}))

    def test_unknown_arguments_and_accounts_are_denied(self):
        self.assertIsNone(self.check("open_photo", {"chat_id": CHAT, "message_id": 1, "save_path": "/tmp/x"}))
        self.assertIsNone(self.check("transcribe_voice", {"chat_id": CHAT, "message_id": 1, "engine": "groq"}))
        self.assertIsNone(self.check("get_history", {"chat_id": CHAT, "account": "other"}))
        self.assertIsNone(self.check("send_message", {"chat_id": CHAT, "message": "x", "custom_emojis": {}}))

    def test_limits_are_capped(self):
        self.assertEqual(self.check("get_history", {"chat_id": CHAT, "limit": 10_000})["limit"], 100)
        self.assertIsNone(self.check("get_history", {"chat_id": CHAT, "limit": -1}))
        self.assertIsNone(self.check("get_history", {"chat_id": CHAT, "limit": "9999"}))

    def test_get_user_status_only_of_the_dm_partner(self):
        self.assertEqual(policy.check("chat", "get_user_status", {"user_id": "77"}, 77, True), {"user_id": 77})
        self.assertIsNone(policy.check("chat", "get_user_status", {"user_id": 78}, 77, True))
        self.assertIsNone(policy.check("chat", "get_user_status", {"user_id": CHAT}, CHAT, False))

    def test_get_me_takes_no_arguments(self):
        self.assertEqual(self.check("get_me", {"account": None}), {})

    def test_blocked_for_everyone(self):
        for scope in ("chat", "owner"):
            for name in policy.BLOCKED:
                self.assertIsNone(policy.check(scope, name, {}, CHAT, False))

    def test_offered_catalogs(self):
        names = {"get_history", "send_message", "list_chats", "forward_message", "leave_chat", "wait_for_events"}
        self.assertEqual(policy.offered("chat", names), {"get_history", "send_message"})
        self.assertEqual(policy.offered("owner", names), {"get_history", "send_message", "list_chats", "forward_message"})


class OwnerScope(unittest.TestCase):
    def test_owner_may_do_anything_but_blocked(self):
        self.assertEqual(policy.check("owner", "leave_chat", {"chat_id": 5}, NOTIFY, True), {"chat_id": 5})

    def test_stays_in_chat(self):
        self.assertTrue(policy.stays_in_chat("get_history", {"chat_id": NOTIFY}, NOTIFY))
        self.assertTrue(policy.stays_in_chat("send_typing", {"chat_id": str(NOTIFY)}, NOTIFY))
        self.assertFalse(policy.stays_in_chat("get_history", {"chat_id": 5}, NOTIFY))
        self.assertFalse(policy.stays_in_chat("list_chats", {}, NOTIFY))
        self.assertFalse(policy.stays_in_chat("send_message", {"chat_id": 5, "message": "x"}, NOTIFY))


class Notify(unittest.TestCase):
    def test_target(self):
        self.assertEqual(policy.notify_target({"OWNER_ID": "100", "NOTIFY_CHAT": ""}), 100)
        self.assertEqual(policy.notify_target({"OWNER_ID": "100", "NOTIFY_CHAT": "300"}), 300)


if __name__ == "__main__":
    unittest.main()
