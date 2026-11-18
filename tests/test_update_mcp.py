"""Tests for update_mcp.py: release check, notification once, upgrade with rollback.

A temporary git repo plays upstream; systemd and the MCP server are faked.
Run: python3 -m unittest discover -s tests -v
"""
import importlib.util, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

AGENT = Path(__file__).resolve().parent.parent / "agent"
spec = importlib.util.spec_from_file_location("update_mcp", AGENT / "update_mcp.py")
U = importlib.util.module_from_spec(spec)
spec.loader.exec_module(U)

CFG = {"OWNER_ID": "1", "SELF_ID": "2", "NOTIFY_CHAT": "1"}


def run(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": cwd})


class FakeMcp:
    """send_message answers plain text, as upstream does (not JSON)."""

    def __init__(self, fail=False, reply="Message sent successfully. message_id=5"):
        self.sent, self.fail, self.reply = [], fail, reply

    def call(self, name, args, timeout=60, raw=False):
        if self.fail:
            raise RuntimeError("connection refused")
        assert raw, "send_message answers text: call it with raw=True"
        self.sent.append((name, args))
        return self.reply


class Repo(unittest.TestCase):
    """upstream: v1.0.0, then PR #5 merged and tagged v1.1.0; the live clone is at v1.0.0."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        up, live = t / "up", t / "live"
        up.mkdir()
        run(up, "git", "init", "-q", "-b", "main")
        (up / "a").write_text("1")
        run(up, "git", "add", "a")
        run(up, "git", "commit", "-qm", "init")
        run(up, "git", "tag", "v1.0.0")
        run(up, "git", "checkout", "-qb", "feat")
        (up / "a").write_text("2")
        run(up, "git", "commit", "-qam", "wip")
        run(up, "git", "checkout", "-q", "main")
        run(up, "git", "merge", "-q", "--no-ff", "feat", "-m",
            "Merge pull request #5 from x/feat\n\nfeat(messages): new field")
        run(up, "git", "tag", "v1.1.0")
        run(up, "git", "tag", "not-a-release")
        run(t, "git", "clone", "-q", str(up), str(live))
        run(live, "git", "-c", "advice.detachedHead=false", "checkout", "-q", "v1.0.0")
        self.up = up
        patches = [mock.patch.object(U, "MCP_DIR", live),
                   mock.patch.object(U, "STATE", t / "state/mcp_update.json"),
                   mock.patch.object(U, "LOCK", t / "state/mcp_update.lock"),
                   mock.patch.object(U.D, "PAUSE", t / "pause"),
                   mock.patch.object(U.D, "PAUSE_ACK", t / "pause.ack"),
                   mock.patch.object(U, "log", lambda *a: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()


class Versions(unittest.TestCase):
    def test_newest_is_numeric_not_lexical(self):
        self.assertEqual(U.newest(["v3.3.9", "v3.3.10", "v3.2.99", "latest", "v4.0.0-rc1"]), "v3.3.10")
        self.assertIsNone(U.newest(["foo"]))


class Check(Repo):
    def test_new_release_notifies_once(self):
        mcp = FakeMcp()
        self.assertEqual(U.check(mcp, CFG), 0)
        self.assertEqual(len(mcp.sent), 1)
        name, args = mcp.sent[0]
        self.assertEqual((name, args["chat_id"]), ("send_message", 1))
        self.assertIn("вышла v1.1.0 (установлена v1.0.0)", args["message"])
        self.assertIn("#5 feat(messages): new field", args["message"])
        self.assertIn("apply v1.1.0", args["message"])
        self.assertEqual(U.check(mcp, CFG), 0)
        self.assertEqual(len(mcp.sent), 1, "the same release is reported once")

    def test_failed_message_is_retried_next_time(self):
        self.assertEqual(U.check(FakeMcp(fail=True), CFG), 1)
        self.assertEqual(U.check(FakeMcp(reply="Error: chat not found"), CFG), 1)
        mcp = FakeMcp()
        U.check(mcp, CFG)
        self.assertEqual(len(mcp.sent), 1)

    def test_up_to_date_sends_nothing(self):
        run(U.MCP_DIR, "git", "-c", "advice.detachedHead=false", "checkout", "-q", "v1.1.0")
        mcp = FakeMcp()
        self.assertEqual(U.check(mcp, CFG), 0)
        self.assertEqual(mcp.sent, [])

    def test_unset_notify_chat_only_logs(self):
        mcp = FakeMcp()
        self.assertEqual(U.check(mcp, {"NOTIFY_CHAT": "<owner_user_id>"}), 0)
        self.assertEqual(mcp.sent, [])


class Apply(Repo):
    def fake(self, fail_on=None):
        """Fake systemd/uv/server; fail_on = the version whose smoke test fails."""
        self.calls = []

        def switch(ref):
            self.calls.append(("switch", ref))
            U.git("-c", "advice.detachedHead=false", "checkout", "--quiet", ref)

        def smoke(cfg, mcp=None, wait=0):
            tag = U.installed()[0]
            self.calls.append(("smoke", tag))
            if tag == fail_on:
                raise RuntimeError("bad format")

        def systemctl(*args, check=True):
            self.calls.append(("systemctl",) + args)
            return "active" if args[0] == "is-active" else ""

        for name, f in (("switch", switch), ("smoke", smoke), ("systemctl", systemctl),
                        ("pause_agent", lambda: self.calls.append(("pause",)) or True)):
            p = mock.patch.object(U, name, f)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(U.D, "load_config", lambda: CFG), mock.patch.object(U.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def test_upgrade(self):
        self.fake()
        self.assertEqual(U.apply(assume_yes=True), 0)
        self.assertEqual(U.installed()[0], "v1.1.0")
        self.assertIn(("systemctl", "stop", "telegram-agent"), self.calls)
        self.assertEqual(self.calls[-2:], [("systemctl", "start", "telegram-agent"),
                                           ("systemctl", "is-active", "telegram-agent")])

    def test_broken_release_rolls_back(self):
        self.fake(fail_on="v1.1.0")
        self.assertEqual(U.apply(assume_yes=True), 1)
        self.assertEqual(U.installed()[0], "v1.0.0")
        self.assertEqual([c for c in self.calls if c[0] == "switch"], [("switch", "v1.1.0"), ("switch", "v1.0.0")])
        self.assertEqual(self.calls[-2], ("systemctl", "start", "telegram-agent"))
        i = self.calls.index(("switch", "v1.0.0"))
        self.assertEqual(self.calls[i - 1], ("systemctl", "stop", "telegram-agent"),
                         "the agent is stopped before the rollback touches the checkout")

    def test_broken_current_version_is_not_touched(self):
        self.fake(fail_on="v1.0.0")
        self.assertEqual(U.apply(assume_yes=True), 1)
        self.assertNotIn("switch", [c[0] for c in self.calls])

    def test_local_changes_block_the_upgrade(self):
        self.fake()
        (U.MCP_DIR / "a").write_text("edited")
        self.assertEqual(U.apply(assume_yes=True), 1)
        self.assertEqual(self.calls, [])

    def test_second_apply_waits_for_the_first(self):
        self.fake()
        U.LOCK.parent.mkdir(exist_ok=True)
        with U.LOCK.open("w") as held:
            U.fcntl.flock(held, U.fcntl.LOCK_EX)
            self.assertEqual(U.apply(assume_yes=True), 1)
        self.assertEqual(self.calls, [])

    def test_pause_is_lifted_afterwards(self):
        self.fake(fail_on="v1.1.0")
        U.D.PAUSE.touch()
        U.apply(assume_yes=True)
        self.assertFalse(U.D.PAUSE.exists())

    def test_unknown_tag(self):
        self.fake()
        self.assertEqual(U.apply("v9.9.9", assume_yes=True), 1)
        self.assertEqual(U.installed()[0], "v1.0.0")


if __name__ == "__main__":
    unittest.main()
