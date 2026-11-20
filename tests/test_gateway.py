"""Verify the scope at the HTTP boundary and the adapter's fail-closed statuses."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from gateway import Gateway, TelegramScope, TEXT_PAGE, text_result
from harnesses import machine_success, opencode_config, opencode_header_timeout
from web_research import Results, contains_private_identifier, fetch_sources, requested_query


class Upstream:
    def __init__(self):
        self.calls = []

    def catalog(self):
        names = ("get_me", "get_history", "get_user_status", "send_message", "send_typing", "open_photo",
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

    def followup_scope(self, message_id, **options):
        state = self.memory.parent / "state"
        state.mkdir(exist_ok=True)
        cfg = dict(OWNER_ID="101", SELF_ID="5", WEB_SEARCH="duckduckgo", **options)
        return TelegramScope(self.upstream, 101, "owner", cfg, self.memory, followup_ids=(message_id,))

    def test_followup_restores_old_request_and_records_one_confirmed_delivery(self):
        first = self.followup_scope(40)
        self.assertFalse(first.call("owner_followup", {"action": "remember"})["isError"])
        path = first.followup_store.path
        saved = json.loads(path.read_text())
        self.assertEqual(saved, {"identity": {"owner": "101", "account": "5"},
                                 "message_ids": [40], "status": "pending"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with patch.object(self.upstream, "tool_result",
                          return_value=text_result("Message sent successfully. message_id=41")):
            first.call("send_message", {"chat_id": 101, "message": "Что написать?"})
        self.assertEqual(json.loads(path.read_text())["question_id"], 41)
        message = {"id": 40, "sender_id": 101, "text": "Напиши @sample_recipient"}
        question = {"id": 41, "sender_id": 5, "out": True, "text": "Что написать?"}

        def lookup(name, args, timeout):
            self.assertEqual((name, args["chat_id"], args["limit"], timeout), ("get_messages_after", 101, 1, 2))
            return {"chat_id": 101, "messages": [{39: message, 40: question}[args["after_id"]]]}

        second = self.followup_scope(200)
        with patch.object(self.upstream, "call", side_effect=lookup, create=True) as call:
            restored = second.call("owner_followup", {"action": "read"})
            self.assertEqual(call.call_count, 2)
        fields = json.loads(restored["content"][0]["text"])
        self.assertEqual([(row["author"], row["text"]) for row in fields["messages"]],
                         [("owner", message["text"]), ("bot", question["text"])])
        self.assertTrue(second.call("search_web", {"query": "Python docs"})["isError"])

        def deliver(name, args, timeout):
            self.assertEqual(json.loads(path.read_text())["status"], "delivery_unknown")
            return text_result("Message sent successfully. message_id=300")

        args = {"chat_id": 202, "message": "Привет!"}
        with patch.object(self.upstream, "tool_result", side_effect=deliver) as tool:
            self.assertTrue(second.call("send_message", dict(args, account="other"))["isError"])
            self.assertTrue(second.call("forward_message", {"to_chat_id": 202})["isError"])
            self.assertFalse(second.call("send_message", args)["isError"])
            second.call("owner_followup", {"action": "clear"})
            self.assertTrue(second.call("send_message", args)["isError"])
            tool.assert_called_once()
        self.assertEqual(json.loads(path.read_text())["delivered_id"], 300)

        third = self.followup_scope(201)
        with patch.object(self.upstream, "call", side_effect=lookup, create=True):
            synced = third.call("owner_followup", {"action": "read"})
        self.assertEqual(json.loads(synced["content"][0]["text"])["status"], "sent")
        self.assertTrue(third.call("send_message", args)["isError"])
        self.assertFalse(third.call("owner_followup", {"action": "clear"})["isError"])
        self.assertFalse(path.exists())

    def test_followup_with_failed_lookup_or_ambiguous_delivery_does_not_resend(self):
        first = self.followup_scope(40)
        self.assertFalse(first.call("owner_followup", {"action": "remember"})["isError"])
        args = {"chat_id": 202, "message": "Привет!"}
        stale = self.followup_scope(100)
        wrong = {"chat_id": 101, "messages": [{"id": 41, "sender_id": 101, "text": "Other message"}]}
        with patch.object(self.upstream, "call", return_value=wrong, create=True):
            self.assertTrue(stale.call("owner_followup", {"action": "read"})["isError"])
        self.assertTrue(stale.call("send_message", args)["isError"])
        self.assertEqual(self.upstream.calls, [])

        valid = {"chat_id": 101, "messages": [{"id": 40, "sender_id": 101, "text": "Напиши контактy"}]}
        with patch.object(self.upstream, "call", return_value=valid, create=True):
            active = self.followup_scope(101)
            self.assertFalse(active.call("owner_followup", {"action": "read"})["isError"])
            with patch.object(self.upstream, "tool_result", side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    active.call("send_message", args)
            retry = self.followup_scope(102)
            restored = retry.call("owner_followup", {"action": "read"})
            self.assertEqual(json.loads(restored["content"][0]["text"])["status"], "delivery_unknown")
            self.assertTrue(retry.call("send_message", args)["isError"])
        self.assertEqual(self.upstream.calls, [])

    def test_followup_tool_is_scoped_to_owner_private_chat(self):
        cfg = {"OWNER_ID": "101", "SELF_ID": "5"}
        for cid, tier, research in ((-100, "owner", False), (101, "guest", False), (101, "owner", True)):
            scope = TelegramScope(self.upstream, cid, tier, cfg, self.memory, research=research, followup_ids=(40,))
            self.assertNotIn("owner_followup", scope.catalog)

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
        config = opencode_config("http://127.0.0.1:8765/test", ["get_history"], 5,
                                 {"MODEL": "opencode-go/qwen", "OPENCODE_HEADER_TIMEOUT_MS": "15000"})
        self.assertEqual([(rule["action"], rule["effect"]) for rule in config["permissions"]],
                         [("*", "deny"), ("telegram_get_history", "allow")])
        self.assertEqual(config["providers"]["opencode-go"]["settings"],
                         {"headerTimeout": 15000})
        self.assertTrue(opencode_header_timeout([
            '{"type":"error","error":{"data":{"message":"Provider response headers timed out"}}}']))
        self.assertTrue(opencode_header_timeout([
            '{"type":"error","error":{"type":"provider.transport","message":"Timed out waiting for response headers"}}']))
        self.assertFalse(opencode_header_timeout([
            '{"type":"text","part":{"text":"Provider response headers timed out"}}']))

    def test_web_research_can_only_reply_to_the_requesting_chat(self):
        scope = TelegramScope(self.upstream, 101, "owner", {"SELF_ID": "5", "NOTIFY_CHAT": "me"},
                              self.memory, research=True)
        self.assertEqual(set(scope.catalog), {"send_message"})
        for name, args in (
            ("send_message", {"chat_id": 100}), ("send_message", {"chat_id": "me"}),
            ("send_message", {"chat_id": 101, "account": "other"}),
            ("send_message", {"chat_id": 101, "send_as": "other"}),
            ("get_history", {"chat_id": 101}), ("memory_read", {}),
            ("call_telegram_tool", {"name": "search_contacts", "arguments": {}}),
        ):
            self.assertTrue(scope.call(name, args)["isError"])
        self.assertEqual(self.upstream.calls, [])
        self.assertFalse(scope.call("send_message", {"chat_id": "101", "message": "Answer"})["isError"])
        self.assertTrue(scope.call("send_message", {"chat_id": 101, "message": "Duplicate"})["isError"])
        self.assertEqual(self.upstream.calls, [("send_message", {"chat_id": 101, "message": "Answer"})])

    def test_web_search_intent_and_source_links(self):
        self.assertIsNone(requested_query({"text": "В цитате есть /web конфиденциальная тема"}))
        self.assertIsNone(requested_query({"text": "/website"}))
        self.assertIsNone(requested_query({"text": "/web forwarded", "forwarded": True}))
        self.assertIsNone(requested_query({"text": "/web quoted", "formatting": [{"type": "blockquote"}]}))
        self.assertEqual(requested_query({"text": "/web Python\n  версии"}), "Python версии")
        self.assertIsNone(requested_query({"text": "Поищи в интернете — последняя версия Python"}))
        self.assertIsNone(requested_query({"text": "Search the web for the latest Python release"}))
        parser = Results()
        parser.feed("""<a class='result-link' href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fpython.org">
                    Python &amp; docs</a><td class='result-snippet'>Stable <b>releases</b></td>
                    <a class='result-link' href="javascript:alert(1)">Drop this</a>
                    <td class="result-snippet">Also drop this</td>""")
        self.assertEqual(parser.results, [{"url": "https://python.org", "title": "Python & docs",
                                           "snippet": "Stable releases"}])

    @patch("web_research.fetch_firecrawl", return_value=([{"url": "https://python.org"}], ""))
    @patch("web_research.fetch", return_value=([], "HTTP 202"))
    def test_search_falls_back_only_when_enabled_and_primary_fails(self, primary, fallback):
        query = "Python release support"
        self.assertEqual(fetch_sources(query, False), ([], "HTTP 202"))
        fallback.assert_not_called()
        self.assertEqual(fetch_sources(query, True), ([{"url": "https://python.org"}], ""))
        fallback.assert_called_once_with(query)
        primary.return_value = ([{"url": "https://peps.python.org"}], "")
        self.assertEqual(fetch_sources(query, True), primary.return_value)
        self.assertEqual(fallback.call_count, 1)

    @patch("gateway.web_research.search", return_value=([{"url": "https://python.org"}], ""))
    def test_automatic_web_search_accepts_a_generated_query_and_then_restricts_tools(self, search):
        cfg = {"SELF_ID": "5", "OWNER_ID": "101", "NOTIFY_CHAT": "me", "WEB_SEARCH": "duckduckgo",
               "WEB_SEARCH_FALLBACK": "firecrawl"}
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
        self.assertIn("search_web", scope.catalog)
        self.assertNotIn("search_web", TelegramScope(
            self.upstream, -100, "owner", cfg, self.memory).catalog)
        self.assertNotIn("search_web", TelegramScope(
            self.upstream, 101, "guest", cfg, self.memory).catalog)
        self.assertNotIn("search_web", self.scope("owner").catalog)
        self.assertFalse(scope.call("get_history", {"chat_id": 101})["isError"])
        for query in (None, {}, "   ", "x" * 401):
            self.assertTrue(scope.call("search_web", {"query": query})["isError"])
        self.assertTrue(scope.no_effects_attempted())
        self.assertFalse(scope.call("search_web", {"query": " Python 3.13  end of life\nPEP 719 "})["isError"])
        search.assert_called_once_with("Python 3.13 end of life PEP 719", fallback=True)
        self.assertFalse(scope.no_effects_attempted())
        self.assertTrue(scope.no_effects_attempted(allow_search=True))
        for name, args in (("memory_write", {"text": "untrusted"}),
                           ("call_telegram_tool", {"name": "send_message", "arguments": {"chat_id": 9}}),
                           ("get_history", {"chat_id": 101}), ("send_message", {"chat_id": 9, "message": "no"})):
            self.assertTrue(scope.call(name, args)["isError"])
        self.assertFalse(scope.call("search_web", {"query": "последняя версия Python"})["isError"])
        self.assertTrue(scope.call("search_web", {"query": "Python"})["isError"])
        self.assertFalse(scope.call("send_message", {"chat_id": 101, "message": "summary"})["isError"])
        self.assertTrue(scope.call("send_message", {"chat_id": 101, "message": "duplicate"})["isError"])

    @patch("gateway.web_research.search")
    def test_web_rejects_private_identifiers_and_context_outside_owner_dm(self, search):
        cfg = {"OWNER_ID": "101", "OWNER_USERNAME": "private_username", "WEB_SEARCH": "duckduckgo"}
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
        for query in ("private_username recent disputes", "owner@example.com", "8 (999) 123-45-67",
                      "connect to 192.168.1.2", "password: syntheticsecret", "ghp_exampletokenonly"):
            self.assertTrue(scope.call("search_web", {"query": query})["isError"])
        for name, args in (("get_history", {"chat_id": 900}), ("memory_read", {})):
            scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
            self.assertFalse(scope.call(name, args)["isError"])
            self.assertTrue(scope.call("search_web", {"query": "Python release support"})["isError"])
        search.assert_not_called()
        self.assertFalse(contains_private_identifier("Python releases 2026-11-20"))

    @patch("gateway.web_research.search", return_value=([{"url": "https://example.com"}], ""))
    def test_public_model_and_comparison_use_two_distinct_queries(self, search):
        cfg = {"OWNER_ID": "101", "MODEL": "provider/qwen3.8-flash", "WEB_SEARCH_PUBLIC_MODEL": "1",
               "WEB_SEARCH": "duckduckgo"}
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)

        first = scope.call("search_web", {"query": "GPT-6.2 Sol OpenAI"})
        self.assertFalse(first["isError"])
        self.assertEqual(scope.call("search_web", {"query": "gpt-6.2 sol openai"}), first)
        second = scope.call("search_web", {"query": "@model"})
        self.assertFalse(second["isError"])
        self.assertEqual(scope.call("search_web", {"query": "Qwen3.8 Flash"}), second)
        self.assertTrue(scope.call("search_web", {"query": "по цене"})["isError"])
        self.assertEqual([call.args for call in search.call_args_list],
                         [("GPT-6.2 Sol OpenAI",), ("qwen3.8-flash",)])

        cfg["MODEL"] = "other-provider/another-model"
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
        self.assertFalse(scope.call("search_web", {"query": "@model"})["isError"])
        search.assert_called_with("another-model", fallback=False)

    @patch("gateway.web_research.search")
    def test_model_search_requires_opt_in_and_an_owner_dm(self, search):
        cfg = {"OWNER_ID": "101", "MODEL": "provider/private-model", "WEB_SEARCH": "duckduckgo"}
        args = {"query": "@model"}
        for options, cid, tier in (
            (cfg, 101, "owner"),
            (dict(cfg, WEB_SEARCH_PUBLIC_MODEL="1"), 101, "guest"),
            (dict(cfg, WEB_SEARCH_PUBLIC_MODEL="1"), -100, "owner"),
            (dict(cfg, WEB_SEARCH_PUBLIC_MODEL="1", MODEL="provider/not a model ID"), 101, "owner"),
        ):
            scope = TelegramScope(self.upstream, cid, tier, options, self.memory)
            self.assertTrue(scope.call("search_web", args)["isError"])
        search.assert_not_called()

    @patch("gateway.web_research.search")
    def test_telegram_mutation_blocks_web_lookup_and_backend_retry(self, search):
        cfg = {"SELF_ID": "5", "OWNER_ID": "101", "WEB_SEARCH": "duckduckgo"}
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
        self.assertFalse(scope.call("memory_write", {"text": "Owner rule"})["isError"])
        self.assertFalse(scope.no_effects_attempted())
        self.assertTrue(scope.call("search_web", {"query": "Python"})["isError"])
        search.assert_not_called()

    @patch("gateway.web_research.search", return_value=([{"url": "https://openai.com"}], ""))
    def test_typing_keeps_search_and_reply_available(self, search):
        cfg = {"SELF_ID": "5", "OWNER_ID": "101", "WEB_SEARCH": "duckduckgo"}
        scope = TelegramScope(self.upstream, 101, "owner", cfg, self.memory)
        self.assertFalse(scope.call("send_typing", {"chat_id": 101})["isError"])
        self.assertFalse(scope.no_effects_attempted())
        self.assertFalse(scope.call("search_web", {"query": "GPT-6.2 Sol OpenAI"})["isError"])
        search.assert_called_once_with("GPT-6.2 Sol OpenAI", fallback=False)
        self.assertFalse(scope.call("send_message", {"chat_id": 101, "message": "summary"})["isError"])
        self.assertTrue(scope.call("send_message", {"chat_id": 101, "message": "duplicate"})["isError"])
