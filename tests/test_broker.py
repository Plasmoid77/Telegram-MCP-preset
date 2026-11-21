"""broker.py: the per-run MCP server every harness talks to. Fake upstream, real HTTP."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CFG, FakeUpstream  # noqa: E402
import broker  # noqa: E402

DM, GROUP, OWNER = 77, -100123, 100


def run(scope="chat", chat=DM, private=True, upstream=None, cfg=CFG, **kw):
    return broker.Run(upstream or FakeUpstream(), cfg, chat=chat, scope=scope, private=private, **kw)


def text(result):
    return result["content"][0]["text"]


class Catalog(unittest.TestCase):
    def test_guest_catalog_has_no_account_tools(self):
        names = set(run().catalog)
        self.assertIn("send_message", names)
        self.assertIn("notify_owner", names)
        self.assertIn("search_web", names)
        for n in ("list_chats", "forward_message", "memory_write", "call_telegram_tool", "read_file",
                  "wait_for_events", "disable_incoming_feed", "leave_chat"):
            self.assertNotIn(n, names)

    def test_owner_catalog(self):
        names = set(run("owner", chat=OWNER, memory=Path("/nonexistent/m.md"), files_dir="/tmp").catalog)
        for n in ("list_chats", "forward_message", "memory_write", "find_telegram_tool", "call_telegram_tool",
                  "read_file", "search_web"):
            self.assertIn(n, names)
        self.assertNotIn("notify_owner", names)
        self.assertNotIn("leave_chat", names)  # reachable through call_telegram_tool only
        self.assertNotIn("wait_for_events", names)

    def test_one_type_per_argument(self):
        # Qwen passes "458" for `integer | array` / `integer | null` and OpenCode rejects the call
        fwd = {"name": "forward_message", "description": "f", "inputSchema": {"type": "object", "properties": {
            "from_chat_id": {"anyOf": [{"type": "integer"}, {"type": "string"}], "title": "From"},
            "message_id": {"anyOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}}]},
            "topic_id": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None, "title": "Topic"},
            "silent": {"type": "boolean", "default": False}}, "required": ["from_chat_id", "message_id"]}}

        class Up(FakeUpstream):
            def catalog(self):
                return [fwd]

        r = run("owner", chat=OWNER, upstream=Up())
        props = r.catalog["forward_message"]["inputSchema"]["properties"]
        self.assertEqual(props["message_id"], {"type": "integer"})
        self.assertEqual(props["topic_id"], {"type": "integer", "title": "Topic"})
        self.assertEqual(props["from_chat_id"], fwd["inputSchema"]["properties"]["from_chat_id"])
        self.assertEqual(props["silent"], {"type": "boolean", "default": False})
        self.assertEqual(r.catalog["forward_message"]["inputSchema"]["required"], ["from_chat_id", "message_id"])
        self.assertIn("anyOf", fwd["inputSchema"]["properties"]["message_id"])  # upstream copy untouched
        found = text(r.call("find_telegram_tool", {"query": "forward"}))
        self.assertIn("array", found)  # the full schema stays reachable for call_telegram_tool

    def test_web_search_off(self):
        self.assertNotIn("search_web", run(cfg=dict(CFG, WEB_SEARCH="")).catalog)


class GuestAndGroup(unittest.TestCase):
    def test_guest_other_chat_blocked(self):
        up = FakeUpstream()
        r = run(upstream=up)
        res = r.call("get_history", {"chat_id": 999, "limit": 5})
        self.assertTrue(res["isError"])
        self.assertEqual(up.calls, [])

    def test_group_run_started_by_owner_cannot_reach_other_chats(self):
        up = FakeUpstream()
        r = run(chat=GROUP, private=False, upstream=up)
        for name, args in (("get_history", {"chat_id": OWNER}), ("send_message", {"chat_id": OWNER, "message": "x"}),
                           ("call_telegram_tool", {"name": "list_chats", "arguments": {}}),
                           ("forward_message", {"from_chat_id": GROUP, "message_id": 1, "to_chat_id": 5}),
                           ("get_user_status", {"user_id": GROUP})):
            with self.subTest(name):
                self.assertTrue(r.call(name, args)["isError"])
        self.assertEqual(up.calls, [])
        self.assertFalse(r.call("get_history", {"chat_id": GROUP, "limit": 10})["isError"])
        self.assertEqual(up.calls, [("get_history", {"chat_id": GROUP, "limit": 10})])

    def test_get_me_is_minimal(self):
        up = FakeUpstream()
        self.assertEqual(json.loads(text(run(upstream=up).call("get_me", {}))), {"id": 200})
        self.assertEqual(up.calls, [])

    def test_sent_counts_only_successful_sends_here(self):
        r = run()
        r.call("send_message", {"chat_id": DM, "message": "hi"})
        self.assertEqual((r.sent, r.effects), (1, True))

    def test_reads_are_not_effects(self):
        r = run()
        r.call("get_history", {"chat_id": DM})
        r.call("transcribe_voice", {"chat_id": DM, "message_id": 3})
        self.assertFalse(r.effects)

    def test_notify_owner(self):
        up = FakeUpstream()
        r = run(upstream=up)
        self.assertFalse(r.call("notify_owner", {"text": "Петя просит перезвонить", "message_id": 4})["isError"])
        self.assertEqual(up.calls, [("forward_message", {"from_chat_id": DM, "message_id": 4, "to_chat_id": OWNER}),
                                    ("send_message", {"chat_id": OWNER, "message": "Петя просит перезвонить"})])
        r.call("notify_owner", {"text": "2"})
        self.assertTrue(r.call("notify_owner", {"text": "3"})["isError"])  # per-run limit
        self.assertTrue(r.effects)


class Owner(unittest.TestCase):
    def owner(self, up=None, **kw):
        return run("owner", chat=OWNER, upstream=up or FakeUpstream(), **kw)

    def test_search_then_other_chat_is_blocked(self):
        up = FakeUpstream()
        r = self.owner(up)
        with mock.patch("web.search", return_value=([{"url": "https://e.x", "title": "t", "snippet": "s"}], "")):
            self.assertFalse(r.call("search_web", {"query": "python 3.14 release date"})["isError"])
        self.assertTrue(r.call("get_history", {"chat_id": 5})["isError"])
        self.assertFalse(r.call("send_message", {"chat_id": OWNER, "message": "ответ"})["isError"])
        self.assertEqual([c[0] for c in up.calls], ["send_message"])

    def test_other_chat_then_search_is_blocked(self):
        r = self.owner()
        r.call("get_history", {"chat_id": 5})
        with mock.patch("web.search") as s:
            self.assertTrue(r.call("search_web", {"query": "news"})["isError"])
            s.assert_not_called()

    def test_private_queries_are_refused(self):
        r = self.owner()
        with mock.patch("web.search") as s:
            for q in ("@owner_x biography", "Иван адрес", "call +7 912 345 67 89", "token: abc123", "100 telegram"):
                with self.subTest(q):
                    self.assertTrue(r.call("search_web", {"query": q})["isError"])
            s.assert_not_called()

    def test_call_telegram_tool_goes_through(self):
        up = FakeUpstream()
        r = self.owner(up)
        self.assertFalse(r.call("call_telegram_tool", {"name": "leave_chat", "arguments": {"chat_id": 5}})["isError"])
        self.assertTrue(r.call("call_telegram_tool", {"name": "wait_for_events", "arguments": {}})["isError"])
        self.assertEqual(up.calls, [("leave_chat", {"chat_id": 5})])

    def test_memory_write(self):
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d) / "state" / "memory.md"
            r = self.owner(memory=mem)
            self.assertFalse(r.call("memory_write", {"text": "- (2026-11-21) отвечать кратко"})["isError"])
            self.assertEqual(mem.read_text(), "- (2026-11-21) отвечать кратко\n")
            self.assertEqual(mem.stat().st_mode & 0o777, 0o600)
            self.assertTrue(r.call("memory_write", {"text": "x" * (broker.MAX_MEMORY + 1)})["isError"])

    def test_read_file_is_confined(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "files"
            root.mkdir()
            (root / "export.json").write_text("hello")
            (Path(d) / "secret.txt").write_text("secret")
            r = self.owner(files_dir=str(root))
            self.assertEqual(text(r.call("read_file", {"path": "export.json"})), "hello")
            for p in ("../secret.txt", str(Path(d) / "secret.txt"), "/etc/passwd"):
                with self.subTest(p):
                    self.assertTrue(r.call("read_file", {"path": p})["isError"])
            self.assertTrue(r.left_chat)


class ReviewFixes(unittest.TestCase):
    def test_after_search_no_owner_tool_leaves_the_chat(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.json").write_text("{}")
            r = run("owner", chat=OWNER, files_dir=d, memory=Path(d) / "m.md")
            with mock.patch("web.search", return_value=([], "")):
                r.call("search_web", {"query": "weather"})
            for name, args in (("read_file", {"path": "x.json"}), ("memory_write", {"text": "x"}),
                               ("call_telegram_tool", {"name": "get_history", "arguments": {"chat_id": OWNER}}),
                               ("send_message", {"chat_id": OWNER, "message": "x", "account": "second"})):
                with self.subTest(name):
                    self.assertTrue(r.call(name, args)["isError"])

    def test_send_refused_in_text_is_not_counted(self):
        up = FakeUpstream(answers={"send_message": "Error: chat write forbidden"})
        r = run(upstream=up)
        r.call("send_message", {"chat_id": DM, "message": "x"})
        self.assertEqual(r.sent, 0)
        self.assertTrue(r.call("notify_owner", {"text": "x"})["isError"])

    def test_reply_receipt_counts_as_sent(self):
        r = run(upstream=FakeUpstream(answers={"reply_to_message": "Replied to message 5 in chat 77."}))
        r.call("reply_to_message", {"chat_id": DM, "message_id": 5, "text": "ok"})
        self.assertEqual(r.sent, 1)

    def test_after_search_owner_calls_follow_chat_rules(self):
        up = FakeUpstream()
        r = run("owner", chat=OWNER, upstream=up)
        with mock.patch("web.search", return_value=([], "")):
            r.call("search_web", {"query": "weather"})
        self.assertTrue(r.call("open_photo", {"chat_id": OWNER, "message_id": 1, "save_path": "/tmp/x"})["isError"])
        self.assertFalse(r.call("send_message", {"chat_id": OWNER, "message": "ответ"})["isError"])
        self.assertEqual([c[0] for c in up.calls], ["send_message"])

    def test_notify_me_means_the_bots_saved_messages(self):
        up = FakeUpstream()
        r = run(upstream=up, cfg=dict(CFG, NOTIFY_CHAT="me"))
        r.call("notify_owner", {"text": "x"})
        self.assertEqual(up.calls, [("send_message", {"chat_id": 200, "message": "x"})])

    def test_call_queued_behind_a_running_one_is_refused_after_close(self):
        started, release = threading.Event(), threading.Event()

        def slow(args):
            started.set()
            release.wait(5)
            return "{}"

        up = FakeUpstream(answers={"get_history": slow})
        r = run(upstream=up)
        first = threading.Thread(target=r.call, args=("get_history", {"chat_id": DM}))
        first.start()
        started.wait(5)
        queued = []
        second = threading.Thread(target=lambda: queued.append(r.call("send_message", {"chat_id": DM, "message": "late"})))
        second.start()
        time.sleep(0.1)
        r.closed.set()
        release.set()
        first.join(5)
        second.join(5)
        self.assertTrue(queued[0]["isError"])
        self.assertNotIn("send_message", [c[0] for c in up.calls])

    def test_stalled_client_does_not_hold_the_drain(self):
        import socket
        with mock.patch.object(broker.Handler, "timeout", 1):
            b = broker.Broker(run()).__enter__()
            s = socket.create_connection(b.server.server_address)
            s.sendall(b"POST ")  # never finishes the request line
            time.sleep(0.2)
            t0 = time.monotonic()
            b.__exit__(None, None, None)
            self.assertLess(time.monotonic() - t0, 4)
            s.close()


class Pages(unittest.TestCase):
    def test_long_result_is_paged_on_record_lines(self):
        records = [{"id": i, "text": "x" * 50} for i in range(400)]
        up = FakeUpstream(answers={"get_history": {"chat_id": DM, "messages": records}})
        r = run(upstream=up, cfg=dict(CFG, PAGE_CHARS="5000"))
        first = text(r.call("get_history", {"chat_id": DM}))
        self.assertIn("read_result(result_id=", first)
        rid = first.split("result_id='")[1].split("'")[0]
        seen, page = [], first
        while True:
            seen += [json.loads(l)["id"] for l in page.splitlines() if l.startswith('{"id"')]
            if "read_result(" not in page:
                break
            offset = int(page.rsplit("offset=", 1)[1].split(")")[0])
            page = text(r.call("read_result", {"result_id": rid, "offset": offset}))
        self.assertEqual(seen, list(range(400)))

    def test_bad_page_requests(self):
        r = run()
        self.assertTrue(r.call("read_result", {"result_id": "nope", "offset": 0})["isError"])


class Http(unittest.TestCase):
    def rpc(self, url, method, params=None, rid=1):
        req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                                                      "params": params or {}}).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def test_end_to_end_and_secret_route(self):
        r = run()
        with broker.Broker(r) as b:
            tools = self.rpc(b.url, "tools/list")["result"]["tools"]
            self.assertIn("send_message", {t["name"] for t in tools})
            res = self.rpc(b.url, "tools/call", {"name": "get_history", "arguments": {"chat_id": 1}})
            self.assertTrue(res["result"]["isError"])
            wrong = b.url.rsplit("/", 1)[0] + "/guess"
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.rpc(wrong, "tools/list")
            self.assertEqual(e.exception.code, 404)

    def test_exit_waits_for_a_call_in_flight(self):
        started, release, done = threading.Event(), threading.Event(), []

        def slow(args):
            started.set()
            release.wait(5)
            done.append(True)
            return "{}"

        r = run(upstream=FakeUpstream(answers={"get_history": slow}))
        b = broker.Broker(r).__enter__()
        t = threading.Thread(target=lambda: self.rpc(b.url, "tools/call",
                                                     {"name": "get_history", "arguments": {"chat_id": DM}}))
        t.start()
        started.wait(5)
        threading.Timer(0.3, release.set).start()
        t0 = time.monotonic()
        b.__exit__(None, None, None)
        self.assertEqual(done, [True], "exit returned before the admitted call finished")
        self.assertGreater(time.monotonic() - t0, 0.2)
        t.join(5)
        self.assertTrue(r.call("get_history", {"chat_id": DM})["isError"])  # closed: nothing new


if __name__ == "__main__":
    unittest.main()
