"""Tests for the dispatcher's history-based delivery (stdlib only, fake MCP server).

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util, json, os, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

AGENT = Path(__file__).resolve().parent.parent / "agent"
spec = importlib.util.spec_from_file_location("dispatcher", AGENT / "dispatcher.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

OWNER, SELF, GUEST, GROUP, BOT = 1, 2, 3, -100, 4
CFG = {"OWNER_ID": "1", "SELF_ID": "2", "MAX_AGE": "1800", "GROUP_TRIGGERS": "Джарвис",
       "PER_CHAT_PER_HOUR": "20", "TOTAL_PER_HOUR": "80", "RECONCILE_CHATS": "15",
       "RECONCILE_INTERVAL": "180", "RESTART_STALLED_MCP": "1"}


def msg(i, age, out=False, text="hi", sender=OWNER, **kw):
    date = datetime.fromtimestamp(time.time() - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dict(id=i, date=date, out=out, text=text, sender_id=SELF if out else sender,
                sender="me" if out else f"user{sender}", **kw)


class FakeMcp:
    def __init__(self):
        self.chats = {}   # chat id -> {"type": chat_type, "messages": [...]}
        self.bots = set()
        self.dialogs = []  # list_chats records, newest first
        self.events, self.calls = [], []

    def call(self, name, args, timeout=60, raw=False):
        self.calls.append(name)
        if name == "get_messages_after":
            c = self.chats[args["chat_id"]]
            ms = sorted(c["messages"], key=lambda m: m["id"])
            if args.get("after_id") is None:
                page = ms[-args["limit"]:]
                more = len(ms) > len(page)
            else:
                rest = [m for m in ms if m["id"] > args["after_id"]]
                page, more = rest[:args["limit"]], len(rest) > args["limit"]
            return {"chat_id": args["chat_id"], "chat_type": c["type"], "has_more": more, "messages": page}
        if name == "list_chats":
            return json.dumps({"results": self.dialogs})
        if name == "get_chat":
            return {"results": [], "bot": args["chat_id"] in self.bots}
        if name == "wait_for_events":
            evs, self.events = self.events, []
            return {"boot": "b", "last_seq": 10, "events": evs}
        raise AssertionError(name)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mcp = FakeMcp()
        self.cur = D.Cursors(Path(self.tmp.name) / "cursors.json")
        self.cur.since = time.time() - 600
        self.d = D.Dispatcher(self.mcp, self.cur)
        self.d.cfg = dict(CFG)
        self.runs = []
        p = mock.patch.object(D, "run_agent", lambda cid, task, tier, cfg: self.runs.append((cid, tier, task)))
        p.start()
        self.addCleanup(p.stop)
        mock.patch.object(D, "log", lambda *a: None).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.tmp.cleanup()

    def chat(self, cid, ctype, *messages):
        self.mcp.chats[cid] = {"type": ctype, "messages": list(messages)}


class Private(Base):
    def test_hint_answers_once(self):
        self.chat(OWNER, "private", msg(10, 20))
        self.d.process(OWNER, "feed")
        self.d.process(OWNER, "feed")
        self.assertEqual([(c, t) for c, t, _ in self.runs], [(OWNER, "owner")])
        self.assertEqual(self.cur.chats[OWNER], 10)

    def test_feed_max_does_not_hide_an_earlier_lost_message(self):
        self.cur.chats[OWNER] = 100
        self.chat(OWNER, "private", msg(101, 300), msg(102, 10))
        self.d.process(OWNER, "feed")
        self.assertIn("id 101..102 (2 шт.)", self.runs[0][2])

    def test_periodic_pass_recovers_a_missed_message(self):
        self.cur.chats[GUEST] = 5
        self.chat(GUEST, "private", msg(5, 900, sender=GUEST), msg(6, 120, sender=GUEST))
        self.mcp.dialogs = [{"chat_id": GUEST, "type": "User"}, {"chat_id": GROUP, "type": "Supergroup"}]
        self.d.periodic()
        self.assertEqual(self.d.due[GUEST][1], "scan")
        self.assertNotIn(GROUP, self.d.due)  # not an allowed group
        self.d.process(GUEST, "scan")
        self.assertEqual([(c, t) for c, t, _ in self.runs], [(GUEST, "guest")])
        self.assertEqual(len(self.d.misses), 1)

    def test_first_look_only_after_last_reply_and_since(self):
        self.chat(GUEST, "private", msg(1, 3000, sender=GUEST), msg(2, 200, sender=GUEST),
                  msg(3, 100, out=True), msg(4, 50, sender=GUEST))
        self.d.process(GUEST, "feed")
        self.assertIn("id 4..4", self.runs[0][2])

    def test_first_look_before_first_run_is_not_answered(self):
        self.cur.since = time.time()
        self.chat(GUEST, "private", msg(7, 60, sender=GUEST))
        self.d.process(GUEST, "scan")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.cur.chats[GUEST], 7)

    def test_stale_messages_are_skipped(self):
        self.cur.chats[OWNER] = 1
        self.chat(OWNER, "private", msg(2, 4000))
        self.d.process(OWNER, "scan")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.cur.chats[OWNER], 2)

    def test_bots_and_self_are_ignored(self):
        self.mcp.bots.add(BOT)
        self.cur.chats[BOT] = 0
        self.chat(BOT, "private", msg(1, 30, sender=BOT))
        self.d.process(BOT, "feed")
        self.chat(SELF, "private", msg(1, 30, sender=SELF))
        self.d.process(SELF, "feed")
        self.chat(777000, "private", msg(1, 30, sender=777000, text="Login code: 12345"))
        self.d.process(777000, "feed")
        self.assertEqual(self.runs, [])
        self.assertIn(BOT, self.cur.bots)

    def test_periodic_pass_lets_a_fresh_burst_settle_once(self):
        self.cur.chats[OWNER] = 0
        self.chat(OWNER, "private", msg(1, 1))
        self.d.process(OWNER, "scan")
        self.assertEqual(self.runs, [])
        self.assertGreater(self.d.due[OWNER][0], time.monotonic())
        self.d.process(OWNER, "scan")  # postponed once only (clock skew cannot stall it)
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.d.misses, [])  # a few seconds old: not a miss

    def test_long_backlog_is_read_in_bounded_passes(self):
        self.cur.chats[OWNER] = 0
        self.chat(OWNER, "private", *[msg(i, 100) for i in range(1, 251)])
        with mock.patch.object(D, "MAX_PAGES", 2):
            self.d.process(OWNER, "feed")
            self.assertEqual(self.cur.chats[OWNER], 200)
            self.assertIn(OWNER, self.d.due)  # the rest is due again
            self.d.process(OWNER, "feed")
        self.assertEqual(self.cur.chats[OWNER], 250)
        self.assertEqual(len(self.runs), 2)

    def test_cursor_saved_before_the_run(self):
        self.chat(OWNER, "private", msg(10, 20))
        D.run_agent = lambda *a: self.runs.append(json.loads(self.cur.path.read_text())["chats"])
        self.d.process(OWNER, "feed")
        self.assertEqual(self.runs, [{str(OWNER): 10}])


    def test_first_look_reads_a_long_burst_whole(self):
        self.chat(GUEST, "private", *[msg(i, 100, sender=GUEST) for i in range(1, 26)])
        self.d.process(GUEST, "feed")
        self.assertIn("id 1..25 (25 шт.)", self.runs[0][2])

    def test_failed_save_keeps_the_old_cursor(self):
        self.cur.chats[OWNER] = 1
        self.chat(OWNER, "private", msg(2, 30))
        with mock.patch.object(self.cur, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.d.process(OWNER, "feed")
        self.assertEqual((self.cur.chats[OWNER], self.runs), (1, []))
        self.d.process(OWNER, "feed")
        self.assertEqual(len(self.runs), 1)


class Groups(Base):
    def setUp(self):
        super().setUp()
        self.d.groups = {GROUP}

    def test_newly_allowed_group_starts_from_now(self):
        self.chat(GROUP, "supergroup", msg(50, 30, addressed=True, sender=GUEST))
        self.d.process(GROUP, "events")
        self.assertEqual(self.runs, [])
        self.assertEqual(self.cur.chats[GROUP], 50)

    def test_only_addressed_or_triggered_messages(self):
        self.cur.chats[GROUP] = 10
        self.chat(GROUP, "supergroup",
                  msg(11, 30, sender=GUEST, text="просто болтовня"),
                  msg(12, 30, sender=GUEST, text="джарвис, привет"),
                  msg(13, 30, sender=OWNER, addressed=True),
                  msg(14, 30, sender=BOT, text="Джарвис"))
        self.mcp.bots.add(BOT)
        self.d.process(GROUP, "events")
        self.assertEqual([(c, t) for c, t, _ in self.runs], [(GROUP, "owner"), (GROUP, "guest")])
        self.assertIn("id 13", self.runs[0][2])
        self.assertIn("id 12", self.runs[1][2])
        self.assertNotIn("id 14", self.runs[1][2])
        self.assertEqual(self.cur.chats[GROUP], 14)

    def test_group_not_allowed_is_ignored(self):
        self.d.groups = set()
        self.cur.chats[GROUP] = 10
        self.chat(GROUP, "supergroup", msg(11, 30, addressed=True, sender=GUEST))
        self.d.process(GROUP, "events")
        self.assertEqual(self.runs, [])


class LiveEvents(Base):
    def test_events_become_settled_hints(self):
        self.d.groups = {GROUP}
        self.d.poll_events(0)  # first call: position only
        self.mcp.events = [
            {"chat_id": GROUP, "chat_type": "supergroup", "message_id": 5},
            {"chat_id": -200, "chat_type": "supergroup", "message_id": 5},   # not allowed
            {"chat_id": BOT, "chat_type": "private", "message_id": 1, "sender_bot": True},
            {"chat_id": GUEST, "chat_type": "private", "message_id": 9, "out": True},
        ]
        self.d.poll_events(0)
        self.assertEqual(set(self.d.pending), {GROUP})
        self.assertIn(BOT, self.cur.bots)
        self.d.pending[GROUP]["last"] -= 10
        self.d.settle_pending()
        self.assertEqual(self.d.due[GROUP][1], "events")

    def test_gap_or_reset_triggers_a_history_pass(self):
        self.d.poll_events(0)
        self.d.next_scan = time.monotonic() + 100
        self.mcp.events = []
        with mock.patch.object(self.mcp, "call", return_value={"boot": "c", "last_seq": 1, "reset": True}):
            self.d.poll_events(0)
        self.assertEqual(self.d.next_scan, 0.0)

    def test_a_live_hint_is_not_counted_as_a_miss(self):
        self.d.hint(OWNER, time.monotonic(), "scan")
        self.d.hint(OWNER, time.monotonic() + 5, "feed")
        self.assertEqual(self.d.due[OWNER][1], "feed")


class Stall(Base):
    def test_restart_after_misses_in_two_passes_without_live_events(self):
        now = time.monotonic()
        self.d.last_live, self.d.misses = now - 500, [now - 300, now - 100]
        with mock.patch.object(D.subprocess, "run") as run:
            self.d.check_stall()
            run.assert_called_once()
            self.assertIn("telegram-mcp.service", run.call_args[0][0])
            self.d.misses = [now - 300, now - 100]
            self.d.check_stall()  # cooldown
            run.assert_called_once()
        self.assertGreater(json.loads(self.cur.path.read_text())["last_restart"], 0)

    def test_no_restart_when_live_events_flow_or_disabled(self):
        now = time.monotonic()
        with mock.patch.object(D.subprocess, "run") as run:
            self.d.last_live, self.d.misses = now - 200, [now - 300, now - 100]
            self.d.check_stall()
            self.d.cfg["RESTART_STALLED_MCP"] = "0"
            self.d.last_live, self.d.misses = now - 500, [now - 300, now - 100]
            self.d.check_stall()
            self.d.cfg["RESTART_STALLED_MCP"] = "1"
            self.d.misses = [now - 100]
            self.d.check_stall()
            run.assert_not_called()


    def test_misses_before_a_live_event_do_not_count(self):
        now = time.monotonic()
        self.d.last_live, self.d.misses = now - 500, [now - 600, now - 300, now - 100]
        with mock.patch.object(D.subprocess, "run") as run:
            self.d.check_stall()
            run.assert_called_once()


class Persist(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "state/cursors.json"
            c = D.Cursors(p)
            self.assertTrue(p.exists())
            c.chats[-100] = 7
            c.bots.add(4)
            c.save()
            c2 = D.Cursors(p)
            self.assertEqual((c2.chats, c2.bots, c2.since), ({-100: 7}, {4}, c.since))


class Pause(Base):
    """update_mcp.py apply: a pause file stops new runs; the ack says none is in progress."""

    def setUp(self):
        super().setUp()
        t = Path(self.tmp.name)
        for name, val in (("PAUSE", t / "pause"), ("PAUSE_ACK", t / "pause.ack"),
                          ("FEED", t / "no-feed"), ("load_config", lambda: dict(CFG)),
                          ("folder_groups", lambda mcp, title: set())):
            mock.patch.object(D, name, val).start()
        self.chat(OWNER, "private", msg(10, 20))
        self.d.next_scan = time.monotonic() + 999
        self.d.hint(OWNER, 0, "feed")

    def test_paused_starts_no_run_and_acks(self):
        D.PAUSE.touch()
        self.d.step()
        self.assertEqual(self.runs, [])
        self.assertTrue(D.PAUSE_ACK.exists())
        self.assertNotIn(OWNER, self.cur.chats, "the cursor stays: the message is answered later")
        D.PAUSE.unlink()
        self.d.step()
        self.assertEqual([(c, t) for c, t, _ in self.runs], [(OWNER, "owner")])

    def test_stale_pause_file_is_ignored(self):
        D.PAUSE.touch()
        old = time.time() - D.PAUSE_MAX - 1
        os.utime(D.PAUSE, (old, old))
        self.d.step()
        self.assertEqual(len(self.runs), 1)

if __name__ == "__main__":
    unittest.main()
