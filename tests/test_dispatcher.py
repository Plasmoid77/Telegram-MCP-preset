"""dispatcher.py: history-based delivery, scopes, limits, prompts. Fake MCP, no network."""
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
import config  # noqa: E402
import dispatcher as D  # noqa: E402

OWNER, SELF, GUEST, GROUP, BOT = 1, 2, 3, -100, 4
CFG = dict(config.DEFAULTS, OWNER_ID="1", SELF_ID="2", GROUP_TRIGGERS="Джарвис", HARNESS="command",
           OWNER_NAME="Иван", OWNER_USERNAME="@owner_x", BOT_NAME="Doom", ACCOUNT_NAME="BotAccount",
           GROUPS=str(GROUP))


def msg(i, age, out=False, text="hi", sender=OWNER, **kw):
    date = datetime.fromtimestamp(time.time() - age, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return dict(id=i, date=date, out=out, text=text, sender_id=SELF if out else sender,
                sender="me" if out else f"user{sender}", **kw)


class FakeMcp:
    def __init__(self):
        self.chats, self.bots, self.dialogs, self.events, self.calls = {}, set(), [], [], []
        self.folders = None  # None: list_folders fails
        self.forwarded, self.forward_error = [], None

    def call(self, name, args, timeout=60, raw=False):
        self.calls.append((name, args))
        if name == "get_messages_after":
            c = self.chats[args["chat_id"]]
            ms = sorted(c["messages"], key=lambda m: m["id"])
            if args.get("after_id") is None:
                page, more = ms[-args["limit"]:], len(ms) > args["limit"]
            else:
                rest = [m for m in ms if m["id"] > args["after_id"]]
                page, more = rest[:args["limit"]], len(rest) > args["limit"]
            return {"chat_id": args["chat_id"], "chat_type": c["type"], "has_more": more, "messages": page}
        if name == "get_message_context":
            return {"chat_id": args["chat_id"], "results": self.chats.get(args["chat_id"], {"messages": []})["messages"]}
        if name == "list_chats":
            if self.dialogs is None:
                raise RuntimeError("down")
            return json.dumps({"results": self.dialogs})
        if name == "get_chat":
            return {"bot": args["chat_id"] in self.bots}
        if name == "wait_for_events":
            evs, self.events = self.events, []
            return {"boot": "b", "last_seq": 10, "events": evs}
        if name == "list_folders":
            if self.folders is None:
                raise RuntimeError("down")
            return json.dumps({"folders": [{"id": 1, "title": "Папка"}]})
        if name == "get_folder":
            return {"included_chats": [{"id": g, "type": "Supergroup"} for g in self.folders]}
        if name == "forward_message":
            self.forwarded.append(args)
            if self.forward_error:
                return self.forward_error
            return (f"Message {args['message_id']} forwarded from {args['from_chat_id']} to {args['to_chat_id']}."
                    + (f" Destination message IDs: [{900 + args['message_id']}]." if args.get("topic_id") else ""))
        raise AssertionError(name)

    def tool_result(self, name, args, timeout=60):
        self.calls.append((name, args))
        return {"content": [{"type": "text", "text": "Message sent. message_id=1"}], "isError": False}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        for name, value in (("PAUSE", t / "pause"), ("PAUSE_ACK", t / "pause.ack"), ("MEMORY", t / "memory.md"),
                            ("CONFIG_DIR", t / "conf")):
            p = mock.patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cfg = dict(CFG)
        patches = [mock.patch.object(D, "log", lambda *a: None),
                   mock.patch.object(config, "load", lambda: dict(self.cfg)),
                   mock.patch.object(D.harnesses, "preflight", lambda cfg: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.mcp = FakeMcp()
        self.cur = D.Cursors(t / "cursors.json")
        self.cur.since = int(time.time()) - 600
        self.runs = []

        def fake_run(url, cfg, kind, cid, new, context, who=None):
            self.runs.append({"kind": kind, "cid": cid, "ids": [m["id"] for m in new],
                              "context": [m["id"] for m in context], "who": who})
            return {"ok": True}

        self.d = D.Dispatcher(self.mcp, self.cur, run=fake_run)
        self.d.cfg = dict(self.cfg)
        self.d.verified = True
        self.d.groups = {GROUP}

    def chat(self, cid, ctype, *messages):
        self.mcp.chats[cid] = {"type": ctype, "messages": list(messages)}


class Delivery(Base):
    def test_owner_dm_runs_in_owner_mode_with_context(self):
        self.cur.chats[OWNER] = 3
        self.chat(OWNER, "private", msg(2, 900, text="напиши Пете"), msg(3, 800, out=True, text="что написать?"),
                  msg(4, 20, text="просто поздоровайся"))
        self.d.process(OWNER, "events")
        self.assertEqual(self.runs, [{"kind": "owner", "cid": OWNER, "ids": [4], "context": [2, 3], "who": None}])
        self.assertEqual(self.cur.chats[OWNER], 4)

    def test_nothing_is_answered_twice(self):
        self.cur.chats[GUEST] = 1
        self.chat(GUEST, "private", msg(1, 900, sender=GUEST), msg(2, 20, sender=GUEST))
        self.d.process(GUEST, "events")
        self.d.process(GUEST, "scan")
        self.assertEqual([r["ids"] for r in self.runs], [[2]])
        self.assertEqual(self.runs[0]["kind"], "guest")
        self.assertEqual(self.runs[0]["who"], {"name": "user3", "username": None})

    def test_first_look_answers_only_after_our_last_reply_and_start(self):
        self.chat(GUEST, "private", msg(1, 5000, sender=GUEST), msg(2, 100, out=True), msg(3, 50, sender=GUEST))
        self.d.process(GUEST, "scan")
        self.assertEqual([r["ids"] for r in self.runs], [[3]])

    def test_group_answers_only_addressed_messages(self):
        self.cur.chats[GROUP] = 1
        self.chat(GROUP, "supergroup", msg(2, 60, sender=GUEST, text="просто болтаем"),
                  msg(3, 50, sender=GUEST, text="эй, Джарвис, что тут?"), msg(4, 40, sender=5, addressed=True),
                  msg(5, 30, sender=BOT, addressed=True))
        self.mcp.bots.add(BOT)
        self.d.process(GROUP, "events")
        self.assertEqual(self.runs[0]["kind"], "group")
        self.assertEqual(self.runs[0]["ids"], [3, 4])

    def test_group_not_allowed_is_ignored_and_new_group_starts_from_now(self):
        self.chat(-200, "supergroup", msg(1, 10, addressed=True))
        self.d.process(-200, "events")
        self.assertNotIn(-200, self.cur.chats)
        self.chat(GROUP, "supergroup", msg(7, 10, sender=GUEST, addressed=True))
        self.d.process(GROUP, "events")
        self.assertEqual((self.runs, self.cur.chats[GROUP]), ([], 7))

    def test_bots_service_chats_and_old_messages_are_skipped(self):
        self.mcp.bots.add(BOT)
        for cid in (BOT, 777000):
            self.cur.chats[cid] = 0
            self.chat(cid, "private", msg(1, 10, sender=cid))
            self.d.process(cid, "events")
        self.cur.chats[GUEST] = 0
        self.chat(GUEST, "private", msg(1, 4000, sender=GUEST))
        self.d.process(GUEST, "events")
        self.assertEqual(self.runs, [])

    def test_scan_waits_while_someone_is_typing(self):
        self.cur.chats[GUEST] = 0
        self.chat(GUEST, "private", msg(1, 1, sender=GUEST))
        self.d.process(GUEST, "scan")
        self.assertEqual(self.runs, [])
        self.assertIn(GUEST, self.d.pending)
        self.assertEqual(self.cur.chats[GUEST], 0)


class ReviewFixes(Base):
    def test_channel_listed_in_groups_is_ignored(self):
        self.d.groups = {-300}
        self.chat(-300, None, msg(1, 10, addressed=True, text="Джарвис"))
        self.d.process(-300, "events")
        self.assertEqual((self.runs, self.cur.chats.get(-300)), ([], None))

    def test_folder_text_error_keeps_group_cursors(self):
        self.cfg.update(GROUPS="", GROUPS_FOLDER="Папка")
        self.cur.chats.update({-500: 9})
        self.mcp.folders = [-500]
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        real = self.mcp.call
        self.mcp.call = lambda n, a, timeout=60, raw=False: ("Error: flood wait 30" if n == "list_folders" else real(n, a, timeout, raw))
        self.d.group_list.read_at = None
        self.d.step()
        self.assertIn(-500, self.cur.chats)
        self.assertEqual(self.d.groups, {-500})

    def test_mention_right_after_a_group_is_allowed_is_answered(self):
        self.d.group_list.allowed_at[GROUP] = time.time() - 30
        self.chat(GROUP, "supergroup", msg(1, 120, sender=GUEST, addressed=True), msg(2, 10, sender=OWNER, addressed=True))
        self.d.process(GROUP, "events")
        self.assertEqual([r["ids"] for r in self.runs], [[2]])

    def test_run_that_never_started_puts_the_burst_back(self):
        self.d.run = lambda *a: {"ok": False, "started": False, "error": "RuntimeError"}
        self.cur.chats[GUEST] = 1
        self.chat(GUEST, "private", msg(1, 900, sender=GUEST), msg(2, 20, sender=GUEST))
        self.d.process(GUEST, "events")
        self.assertEqual(self.cur.chats[GUEST], 1)
        self.assertIn(GUEST, self.d.pending)

    def test_owner_dm_is_always_scanned(self):
        self.mcp.dialogs = [{"chat_id": GUEST, "type": "User"}]
        self.d.periodic()
        self.assertEqual(set(self.d.pending), {GUEST, OWNER, GROUP})

    def test_no_dispatch_until_the_account_is_confirmed(self):
        self.d.verified = False
        self.mcp.me = {"id": 999}
        real = self.mcp.call
        self.mcp.call = lambda n, a, timeout=60, raw=False: self.mcp.me if n == "get_me" else real(n, a, timeout, raw)
        with self.assertRaises(SystemExit):
            self.d.step()
        self.assertEqual([c[0] for c in self.mcp.calls], [])
        self.mcp.me = {"id": 2}
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertTrue(self.d.verified)

    def test_live_position_is_taken_before_the_first_scan(self):
        self.d.next_scan = 0.0
        self.d.step()
        names = [c[0] for c in self.mcp.calls]
        self.assertLess(names.index("wait_for_events"), names.index("list_chats"))

    def test_event_hint_waits_for_a_message_that_just_arrived(self):
        self.cur.chats[GUEST] = 0
        self.chat(GUEST, "private", msg(1, 1, sender=GUEST))
        self.d.process(GUEST, "events")
        self.assertEqual(self.runs, [])
        self.d.waiting[GUEST] -= D.MAX_WAIT  # someone has been typing for too long: answer now
        self.d.process(GUEST, "events")
        self.assertEqual(len(self.runs), 1)


class Limits(Base):
    def test_rate_limited_burst_stays_unread_and_comes_back(self):
        self.cfg.update(PER_CHAT_PER_HOUR="1")
        self.d.cfg = dict(self.cfg)
        self.cur.chats[GUEST] = 0
        self.chat(GUEST, "private", msg(1, 20, sender=GUEST))
        self.d.process(GUEST, "events")
        self.mcp.chats[GUEST]["messages"].append(msg(2, 10, sender=GUEST))
        self.d.process(GUEST, "events")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.cur.chats[GUEST], 1, "the limited message must not be marked as read")
        self.assertGreater(self.d.pending[GUEST]["last"], time.monotonic() + 60)

    def test_guests_cannot_use_up_the_owners_budget(self):
        rl, cfg = D.RateLimit(), dict(CFG, TOTAL_PER_HOUR="2", PER_CHAT_PER_HOUR="5", OWNER_PER_HOUR="1")
        self.assertTrue(rl.allow(GUEST, False, cfg))
        self.assertTrue(rl.allow(5, False, cfg))
        self.assertFalse(rl.allow(6, False, cfg))
        self.assertTrue(rl.allow(OWNER, True, cfg))
        self.assertFalse(rl.allow(OWNER, True, cfg))


class Loop(Base):
    def test_pause_stops_new_runs_and_acks(self):
        config.PAUSE.touch()
        self.cur.chats[GUEST] = 0
        self.chat(GUEST, "private", msg(1, 20, sender=GUEST))
        self.d.hint(GUEST, "events", at=time.monotonic() - 60)
        self.d.next_scan = time.monotonic() + 999
        self.d.seq = 10
        self.d.step()
        self.assertEqual(self.runs, [])
        self.assertTrue(config.PAUSE_ACK.exists())
        config.PAUSE.unlink()
        self.d.step()
        self.assertEqual(len(self.runs), 1)

    def test_events_hint_then_settle(self):
        self.d.seq, self.d.next_scan = 10, time.monotonic() + 999
        self.mcp.events = [{"chat_id": GUEST, "chat_type": "private"}, {"chat_id": -300, "chat_type": "group"},
                           {"chat_id": GUEST, "chat_type": "private", "out": True}]
        self.d.poll_events(0)
        self.assertEqual(set(self.d.pending), {GUEST})
        self.assertEqual(self.d.due(), [])
        self.d.pending[GUEST]["last"] -= D.SETTLE
        self.assertEqual(self.d.due(), [GUEST])

    def test_group_confirmed_off_the_folder_loses_its_cursor(self):
        self.cfg.update(GROUPS="", GROUPS_FOLDER="Папка")
        self.cur.chats.update({-500: 9, GUEST: 3})
        self.mcp.folders = [-500]
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertEqual(self.d.groups, {-500})
        self.mcp.folders = []
        self.d.group_list.read_at = None
        self.d.step()
        self.assertNotIn(-500, self.cur.chats)
        self.assertIn(GUEST, self.cur.chats)

    def test_unreadable_folder_keeps_the_list_for_a_while_then_suspends_it(self):
        g, cfg = D.Groups(), dict(CFG, GROUPS="", GROUPS_FOLDER="Папка")
        self.mcp.folders = [-500]
        self.assertEqual(g.refresh(self.mcp, cfg), ({-500}, True))
        self.mcp.folders, g.read_at = None, None
        ids, confirmed = g.refresh(self.mcp, cfg)
        self.assertEqual((ids, confirmed), ({-500}, True))
        g.ok_at -= D.FOLDER_TTL + 1
        g.read_at = None
        self.assertEqual(g.refresh(self.mcp, cfg), (set(), False))

    def test_folder_never_read_does_not_drop_group_cursors(self):
        self.cfg.update(GROUPS="", GROUPS_FOLDER="Папка")
        self.cur.chats.update({-500: 9})
        self.mcp.folders = None  # unreadable from the start
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertEqual(self.d.groups, set())
        self.assertIn(-500, self.cur.chats)


class Prompts(Base):
    def test_guest_prompt_has_no_memory_and_no_owner_identity(self):
        config.MEMORY.write_text("- секрет владельца")
        for kind in ("guest", "group"):
            with self.subTest(kind):
                text = D.instructions(self.cfg, kind)
                for private in ("секрет владельца", "Иван", "owner_x", "{{"):
                    self.assertNotIn(private, text)
                self.assertIn("Doom", text)

    def test_owner_prompt_has_memory_and_name(self):
        config.MEMORY.write_text("- отвечать кратко")
        text = D.instructions(self.cfg, "owner")
        self.assertIn("отвечать кратко", text)
        self.assertIn("Иван", text)

    def test_persona_override(self):
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / "persona.md").write_text("Ты — {{BOT_NAME}}, пират.")
        self.assertIn("Ты — Doom, пират.", D.instructions(self.cfg, "guest"))

    def test_task_marks_owner_messages(self):
        t = D.task("group", GROUP, [msg(5, 1, text="Джарвис, привет")], [msg(4, 9, sender=GUEST, text="ку")], self.cfg)
        self.assertIn('"from": "owner"', t)
        self.assertIn('"from": "user3"', t)


AVITO, CONCLAVE, TOPIC = 8001, -1009, 68


class Relays(Base):
    def setUp(self):
        super().setUp()
        self.cfg["RELAYS"] = f"{AVITO}>{CONCLAVE}:{TOPIC}~avito\\.ru/\\d{{6,}}"
        self.d.cfg = dict(self.cfg)
        self.d.relays = D.parse_relays(self.cfg["RELAYS"])[0]
        self.mcp.bots.add(AVITO)

    def listing(self, i, age=30, **kw):
        return msg(i, age, sender=AVITO, text=f"HDD 12TB\n18 000 ₽\nСсылка: https://avito.ru/85097{i:05d}", **kw)

    def test_rules(self):
        rules, bad = D.parse_relays("1>-2:5~avito  3>-4 x>1 5>5 6>7~( 8>-9:0 10>-1 10>-2 11>-1~a{99999999999} " + "1" * 5000 + ">-1")
        self.assertEqual(set(rules), {1, 3})
        self.assertEqual(rules[1][:2], (-2, 5))
        self.assertEqual(rules[3], (-4, None, None))
        self.assertEqual(bad, ["x>1", "5>5", "6>7~(", "8>-9:0", "10>-1", "10>-2", "11>-1~a{99999999999}", "1" * 5000 + ">-1"])
        self.assertTrue(D.relay_match({"text": "x", "links": [{"url": "https://AVITO.ru/1"}]}, rules[1][2]))

    def start(self, cursor):
        self.d.relay_state(AVITO)["cursor"] = cursor

    def test_new_rule_starts_from_now(self):
        self.chat(AVITO, "private", self.listing(1, age=60))
        self.cur.relays.clear()
        self.d.relay_state(AVITO)  # activation: the newest message now is the boundary
        self.mcp.chats[AVITO]["messages"] += [self.listing(i, age=5) for i in range(2, 2 + D.NEW_CHAT_PAGE + 3)]
        while self.d.process(AVITO, "scan") is None and AVITO in self.d.pending:
            self.d.pending.clear()
        self.assertEqual([a["message_id"] for a in self.mcp.forwarded], list(range(2, 2 + D.NEW_CHAT_PAGE + 3)))
        self.assertEqual(self.cur.relays[AVITO]["cursor"], 1 + D.NEW_CHAT_PAGE + 3)

    def test_unread_activation_falls_back_to_the_start_time(self):
        self.chat(AVITO, "private", self.listing(1, age=60), self.listing(2, age=50))
        self.cur.relays[AVITO] = {"rule": D.relay_ident(self.d.relays[AVITO]), "since": int(time.time()) - 55,
                                  "cursor": None}
        self.d.process(AVITO, "scan")
        self.assertEqual([a["message_id"] for a in self.mcp.forwarded], [2])
        self.assertEqual(self.cur.relays[AVITO]["cursor"], 2)
        self.mcp.chats[AVITO]["messages"].append(self.listing(9, age=1))
        self.cfg["RELAYS"] = f"{AVITO}>{CONCLAVE}"  # a changed rule starts again from now
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertEqual(self.cur.relays[AVITO]["cursor"], 9)
        self.cur.chats[AVITO] = 3
        self.cfg["RELAYS"] = ""
        self.d.step()
        self.assertEqual((self.cur.relays, self.cur.chats[AVITO]), ({}, 9))

    def test_relay_source_keeps_its_progress_when_group_cursors_are_cleaned(self):
        self.cfg["RELAYS"] = f"{-777}>{CONCLAVE}"
        self.cur.chats[-777] = 5  # an old reply cursor of the same chat does not matter
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.cur.relay_commit(-777, 7)
        self.d.step()
        self.assertEqual(self.cur.relays[-777]["cursor"], 7)
        self.assertEqual(json.loads(self.cur.path.read_text())["relays"]["-777"]["cursor"], 7)

    def test_listings_are_forwarded_once_in_order_and_service_messages_are_not(self):
        self.start(1)
        self.chat(AVITO, "private", self.listing(1), msg(2, 40, out=True, text="https://avito.ru/123456789"),
                  msg(3, 35, sender=AVITO, text="По добавленному поиску 2 объявления",
                      links=[{"url": "https://www.avito.ru/sankt-peterburg/hdd"}]),
                  self.listing(4), self.listing(5, age=7200, media="photo"))
        self.d.process(AVITO, "events")
        self.d.process(AVITO, "scan")
        self.assertEqual([(a["message_id"], a["to_chat_id"], a["topic_id"], a["expand_album"])
                          for a in self.mcp.forwarded], [(4, CONCLAVE, TOPIC, False), (5, CONCLAVE, TOPIC, False)])
        self.assertEqual((self.cur.relays[AVITO]["cursor"], self.runs), (5, []))  # a relay source is never answered
        self.assertEqual(list(self.d.reviews), [])  # no relay_review.md: forwarding only

    def test_bot_source_is_watched_by_events_and_the_scan(self):
        self.d.seq = 10
        self.mcp.events = [{"chat_id": AVITO, "chat_type": "private", "sender_bot": True}]
        self.d.poll_events(0)
        self.assertIn(AVITO, self.d.pending)
        self.d.pending.clear()
        self.cur.bots.add(AVITO)
        self.mcp.dialogs = None  # list_chats breaks: relay sources are still scheduled
        with self.assertRaises(Exception):
            self.d.periodic()
        self.assertIn(AVITO, self.d.pending)

    def test_failed_forward_is_retried_then_skipped_with_a_notice(self):
        self.start(0)
        self.chat(AVITO, "private", self.listing(1), self.listing(2))
        self.mcp.forward_error = "An error occurred (code: FWD-1). Check mcp_errors.log for details."
        for attempt in range(D.RELAY_TRIES - 1):
            self.d.process(AVITO, "events")
            self.assertEqual(self.cur.relays[AVITO]["cursor"], 0)
            self.assertGreaterEqual(self.d.pending[AVITO]["last"] - time.monotonic(), D.RETRY * 2 ** attempt - 1)
        self.d.process(AVITO, "events")
        self.assertEqual([a["chat_id"] for n, a in self.mcp.calls if n == "send_message"], [OWNER])
        self.assertEqual(self.cur.relays[AVITO]["cursor"], 1)  # 1 skipped; 2 failed once and waits
        self.mcp.forward_error = None
        self.d.process(AVITO, "events")
        self.assertEqual(self.cur.relays[AVITO]["cursor"], 2)
        self.assertEqual([a["message_id"] for a in self.mcp.forwarded][-1], 2)

    def test_review_runs_once_per_listing_after_due_chats(self):
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / D.RELAY_REVIEW).write_text("Оцени фото диска.")
        self.start(0)
        self.chat(AVITO, "private", self.listing(1, media="photo"), self.listing(2))
        self.d.process(AVITO, "events")
        self.assertEqual([(c, r["id"], r["topic"], r["forwarded"]) for _, _, c, r, _ in self.d.reviews],
                         [(CONCLAVE, 901, TOPIC, True), (CONCLAVE, 902, TOPIC, True)])
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertEqual(self.runs, [{"kind": "relay", "cid": CONCLAVE, "ids": [901], "context": [], "who": None}])
        self.d.step()
        self.assertEqual([r["ids"] for r in self.runs], [[901], [902]])

    def test_backlog_goes_in_batches_and_reviews_are_dropped_with_their_rule(self):
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / D.RELAY_REVIEW).write_text("Оцени.")
        self.start(0)
        self.chat(AVITO, "private", *[self.listing(i) for i in range(1, D.RELAY_BATCH + 4)])
        self.d.process(AVITO, "events")
        self.assertEqual((len(self.mcp.forwarded), self.cur.relays[AVITO]["cursor"]), (D.RELAY_BATCH, D.RELAY_BATCH))
        self.assertIn(AVITO, self.d.pending)
        self.d.process(AVITO, "events")
        self.assertEqual(len(self.mcp.forwarded), D.RELAY_BATCH + 3)
        self.cfg["RELAYS"] = ""
        self.d.next_scan, self.d.seq = time.monotonic() + 999, 10
        self.d.step()
        self.assertEqual(self.runs, [])

    def test_relay_task_and_prompt(self):
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / D.RELAY_REVIEW).write_text("Оцени фото диска.")
        config.MEMORY.write_text("- секрет владельца")
        rec = dict(self.listing(1), id=901, topic=TOPIC, forwarded=True)
        t = D.task("relay", CONCLAVE, [rec], [], self.cfg)
        for part in ("Оцени фото диска.", '"id": 901', '"forwarded": true', "reply_to"):
            self.assertIn(part, t)
        text = D.instructions(self.cfg, "relay")
        self.assertIn("Пересылка", text)
        for private in ("секрет владельца", "Иван", "owner_x"):
            self.assertNotIn(private, text)


class Execute(Base):
    def setUp(self):
        super().setUp()
        self.upstream = helpers.FakeUpstream()
        p = mock.patch.object(D, "Mcp", lambda url: self.upstream)
        p.start()
        self.addCleanup(p.stop)

    def execute(self, results, kind="owner", send=False):
        calls = []

        def fake(cfg, owner, prompt, text, url, tools, timeout, retry=False):
            calls.append(retry)
            if send:  # the agent sends through the broker before failing
                D.broker.Run.last.call("send_message", {"chat_id": OWNER, "message": "x"})
            return results[len(calls) - 1]

        init = D.broker.Run.__init__

        def remember(run, *a, **kw):
            init(run, *a, **kw)
            D.broker.Run.last = run

        with mock.patch.object(D.harnesses, "run", side_effect=fake), \
                mock.patch.object(D.broker.Run, "__init__", remember):
            s = D.execute("http://x", self.cfg, kind, OWNER if kind == "owner" else GUEST, [msg(1, 1)], [])
        return s, calls

    def test_transient_failure_before_any_effect_is_retried_once(self):
        R = D.harnesses.Result
        s, calls = self.execute([R(False, 1, True), R(True, 0, False)])
        self.assertEqual((s["ok"], calls), (True, [False, True]))

    def test_no_retry_and_no_notice_after_an_effect(self):
        R = D.harnesses.Result
        s, calls = self.execute([R(False, 1, True)], send=True)
        self.assertEqual(calls, [False])
        self.assertEqual([c[0] for c in self.upstream.calls], ["send_message"])  # only the agent's own send

    def test_failed_owner_run_gets_a_notice_guest_does_not(self):
        R = D.harnesses.Result
        fail = [R(False, 1, False)] * 2  # a run that called nothing is retried once
        self.execute(fail)
        self.assertEqual([c[0] for c in self.upstream.calls], ["send_message"])
        self.upstream.calls.clear()
        self.execute(fail, kind="guest")
        self.assertEqual(self.upstream.calls, [])

    def test_open_url_only_in_runs_the_owner_started_and_task_links_are_seen(self):
        R, seen = D.harnesses.Result, []

        def fake(cfg, owner, prompt, text, url, tools, timeout, retry=False):
            seen.append(("open_url" in tools, set(D.broker.Run.last.seen)))
            return R(True, 0, False)

        init = D.broker.Run.__init__

        def remember(run, *a, **kw):
            init(run, *a, **kw)
            D.broker.Run.last = run

        with mock.patch.object(D.harnesses, "run", side_effect=fake), \
                mock.patch.object(D.broker.Run, "__init__", remember):
            context = [msg(1, 9, out=True, text="бот писал https://bot.example/x"),
                       msg(2, 8, sender=GUEST, text="гость", links=[{"text": "тут", "url": "https://g.example/p"}])]
            for sender in (OWNER, GUEST):
                D.execute("http://x", self.cfg, "group", GROUP,
                          [msg(3, 1, sender=sender, text="Джарвис, глянь example.com/doc")], context)
        self.assertEqual(seen, [(True, {"example.com/doc", "g.example/p"}), (False, set())])


if __name__ == "__main__":
    unittest.main()
