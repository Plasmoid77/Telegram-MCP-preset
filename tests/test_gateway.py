"""Verify the scope at the HTTP boundary and the adapter's fail-closed statuses."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from gateway import Gateway, TelegramScope, TEXT_PAGE, text_result
from harnesses import machine_success, opencode_config


class Upstream:
    def __init__(self):
        self.calls = []

    def catalog(self):
        names = ("get_me", "get_history", "get_user_status", "send_message", "open_photo",
                 "transcribe_voice", "forward_message", "list_chats", "search_contacts", "disable_incoming_feed")
        return [{"name": name, "inputSchema": {"type": "object"}} for name in names]

    def tool_result(self, name, args, timeout):
        self.calls.append((name, args))
        if name == "get_history":
            return text_result("a" * TEXT_PAGE + "second page")
        return text_result("upstream response")


def request(url, method, params=None, headers=None):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)["result"]


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.memory = Path(temp.name) / "memory.md"
        self.upstream = Upstream()

    def scope(self, tier):
        return TelegramScope(self.upstream, 101, tier, {"SELF_ID": "5", "NOTIFY_CHAT": "me"}, self.memory)

    def test_guest_scope_checks_required_arguments_and_notification_direction(self):
        with Gateway(self.scope("guest")) as bridge:
            version = request(bridge.url, "initialize", {"protocolVersion": "2025-06-18"})
            self.assertEqual(version["protocolVersion"], "2025-06-18")
            catalog = request(bridge.url, "tools/list")["tools"]
            self.assertNotIn("list_chats", {tool["name"] for tool in catalog})
            for name, args in (
                ("get_history", {}), ("get_history", {"chat_id": 100}),
                ("get_history", {"chat_id": 101, "account": "other"}),
                ("open_photo", {"chat_id": 101, "save_path": "probe"}),
                ("transcribe_voice", {"chat_id": 101, "engine": "openai"}),
                ("get_user_status", {"user_id": 100}),
                ("forward_message", {"from_chat_id": 101, "to_chat_id": 100}),
                ("forward_message", {"from_chat_id": 5, "to_chat_id": "me"}),
                ("send_message", {"chat_id": 100}),
                ("memory_write", {"text": "should not write"}),
                ("list_chats", {}),
            ):
                with self.subTest(name=name, args=args):
                    result = request(bridge.url, "tools/call", {"name": name, "arguments": args})
                    self.assertTrue(result["isError"])
            self.assertEqual(self.upstream.calls, [])
            for chat in ("@101", "me", 5):
                result = request(bridge.url, "tools/call",
                                 {"name": "send_message", "arguments": {"chat_id": chat}})
                self.assertFalse(result["isError"])
            self.assertEqual(self.upstream.calls, [
                ("send_message", {"chat_id": 101}),
                ("send_message", {"chat_id": "me"}),
                ("send_message", {"chat_id": "me"}),
            ])
            with self.assertRaises(urllib.error.HTTPError) as context:
                request(bridge.url, "tools/list", headers={"Origin": "http://example.org"})
            self.assertEqual(context.exception.code, 403)
            context.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as context:
                request(bridge.url + "wrong", "tools/list")
            self.assertEqual(context.exception.code, 404)
            context.exception.close()

    def test_owner_memory_and_result_pages(self):
        scope = self.scope("owner")
        self.assertNotIn("disable_incoming_feed", scope.catalog)
        self.assertFalse(scope.call("memory_write", {"text": "Owner note"})["isError"])
        self.assertEqual(scope.call("memory_read", {})["content"][0]["text"], "Owner note")
        self.assertEqual(self.memory.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("search_contacts", scope.catalog)
        search = scope.call("find_telegram_tool", {"query": "contacts"})
        self.assertEqual(json.loads(search["content"][0]["text"])[0]["name"], "search_contacts")
        self.assertFalse(scope.call("call_telegram_tool",
                                    {"name": "search_contacts", "arguments": {}})["isError"])
        self.assertTrue(scope.call("call_telegram_tool",
                                   {"name": "disable_incoming_feed", "arguments": {}})["isError"])
        self.assertTrue(self.scope("guest").call(
            "call_telegram_tool", {"name": "search_contacts", "arguments": {}})["isError"])
        first = scope.call("get_history", {"chat_id": 100})["content"][0]["text"]
        self.assertNotIn("second page", first)
        result_id = first.split("result_id='")[1].split("'")[0]
        self.assertEqual(scope.call("read_result", {"result_id": result_id, "offset": TEXT_PAGE}),
                         text_result("second page"))
        self.assertTrue(self.scope("guest").call("read_result",
                                                {"result_id": result_id, "offset": TEXT_PAGE})["isError"])

    def test_harness_status_and_opencode_default_deny(self):
        self.assertTrue(machine_success("codex", 0, ['{"type":"turn.completed"}']))
        self.assertFalse(machine_success("codex", 0, ['{"type":"turn.failed"}']))
        self.assertTrue(machine_success("opencode", 0, ['{"type":"step_finish"}']))
        self.assertFalse(machine_success("opencode", 0, ['{"type":"step_finish"}', '{"type":"error"}']))
        config = opencode_config("http://127.0.0.1:8765/test", ["get_history"], 5)
        self.assertEqual([(rule["action"], rule["effect"]) for rule in config["permissions"]],
                         [("*", "deny"), ("telegram_get_history", "allow")])
