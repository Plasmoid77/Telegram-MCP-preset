"""scripts/migrate-v1.sh moves a v1 install to the v2 layout, and its rollback.sh undoes it.

Runs the real script in a fake HOME with stub systemctl/uv; a thread plays the v1 bot that
acknowledges the pause.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in *is-active*telegram-agent*) echo active;; *is-active*) echo inactive;; esac
exit 0
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
class Migrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = self.home = Path(self.tmp.name) / "home"
        share = home / ".local/share"
        self.old = share / "telegram-agent"
        (self.old / "state").mkdir(parents=True)
        for name, text in (("config.env", "OWNER_ID=1\nSELF_ID=2\nHARNESS=opencode\n"), ("backend.env", "K=v\n"),
                           ("prompt.md", "старый промпт"), ("memory.md", "- память\n"), ("dispatcher.py", "v1"),
                           ("mcp.json", '{"mcpServers": {"telegram": {"url": "http://127.0.0.1:8765/mcp"}}}'),
                           ("state/cursors.json", '{"since": 1.5, "chats": {"1": 5}}'),
                           ("state/owner-followup.json", "{}")):
            (self.old / name).write_text(text)
        units = home / ".config/systemd/user"
        units.mkdir(parents=True)
        (units / "telegram-agent.service").write_text("v1 unit")
        mcp = share / "telegram-mcp"
        mcp.mkdir(parents=True)
        git(mcp, "init", "-q")
        git(mcp, "commit", "-q", "--allow-empty", "-m", "x")
        git(mcp, "tag", "v3.3.23")
        (mcp / ".env").write_text("secret")
        self.new = share / "telegram-agent.new"
        files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=REPO, capture_output=True,
                               text=True, check=True).stdout.split()
        for f in files:
            if (REPO / f).is_file():
                (self.new / f).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / f, self.new / f)
        stub = Path(self.tmp.name) / "bin"
        stub.mkdir()
        for name in ("systemctl", "uv"):
            (stub / name).write_text(STUB)
            (stub / name).chmod(0o755)
        self.log = Path(self.tmp.name) / "calls.log"
        self.env = {"HOME": str(home), "PATH": f"{stub}:{os.environ['PATH']}", "STUB_LOG": str(self.log),
                    "TG_MIGRATE_CHECK": "0", "TG_MIGRATE_SETTLE": "0"}

    def bot(self, stop):
        """Plays whichever bot is running: acknowledges a pause in v1's or v2's state dir."""
        dirs = (self.old / "state", self.home / ".local/state/telegram-agent")
        while not stop.is_set():
            for d in dirs:
                if (d / "pause").exists():
                    (d / "pause.ack").touch()
            time.sleep(0.05)

    def test_migrate_and_roll_back(self):
        stop = threading.Event()
        threading.Thread(target=self.bot, args=(stop,), daemon=True).start()
        self.addCleanup(stop.set)
        r = subprocess.run(["bash", str(self.new / "scripts/migrate-v1.sh")], env=self.env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        conf, state = self.home / ".config/telegram-agent", self.home / ".local/state/telegram-agent"
        self.assertEqual((conf / "config.env").read_text(), "OWNER_ID=1\nSELF_ID=2\nHARNESS=opencode\n")
        self.assertEqual((conf / "config.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((conf / "prompt.v1.md").read_text(), "старый промпт")
        self.assertEqual((state / "memory.md").read_text(), "- память\n")
        self.assertEqual(json.loads((state / "cursors.json").read_text())["chats"], {"1": 5})
        self.assertIn("owner follow-up", r.stdout)
        self.assertTrue((self.old / "agent/broker.py").exists(), "v2 checkout moved into place")
        self.assertFalse(self.new.exists())
        backup = next(self.old.parent.glob("telegram-agent.v1-*"))
        self.assertEqual((backup / "dispatcher.py").read_text(), "v1")
        self.assertEqual((backup / "units/telegram-agent.service").read_text(), "v1 unit")
        unit = (self.home / ".config/systemd/user/telegram-agent.service").read_text()
        self.assertIn(f"{self.old}/agent/dispatcher.py", unit)
        self.assertNotIn("@AGENT_DIR@", unit)
        calls = self.log.read_text()
        self.assertIn("--user stop telegram-agent", calls)
        self.assertIn("--user restart telegram-mcp asr-server", calls)
        self.assertIn("--user start telegram-agent", calls)
        self.assertFalse((backup / "state/pause").exists())

        # v2 answers a message and learns something before the owner decides to go back
        (state / "cursors.json").write_text('{"since": 1, "chats": {"1": 9}}')
        (state / "memory.md").write_text("- новая память\n")
        r = subprocess.run(["bash", str(backup / "rollback.sh")], env=self.env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.old / "dispatcher.py").read_text(), "v1")
        self.assertEqual(json.loads((self.old / "state/cursors.json").read_text())["chats"], {"1": 9})
        self.assertEqual((self.old / "memory.md").read_text(), "- новая память\n")
        self.assertTrue(any(p.name.startswith("telegram-agent.v2-") for p in self.old.parent.iterdir()))
        self.assertEqual((self.home / ".config/systemd/user/telegram-agent.service").read_text(), "v1 unit")

    def test_failure_before_the_move_restarts_v1(self):
        (self.new / "install.sh").unlink()  # not reached: the failure below comes first
        self.env["PATH"] = self.env["PATH"].split(":", 1)[1]  # no stub uv: step 2 fails ...
        stub = Path(self.tmp.name) / "bin2"
        stub.mkdir()
        (stub / "systemctl").write_text(STUB)
        (stub / "systemctl").chmod(0o755)
        (stub / "uv").write_text("#!/bin/sh\nexit 3\n")
        (stub / "uv").chmod(0o755)
        self.env["PATH"] = f"{stub}:{self.env['PATH']}"
        r = subprocess.run(["bash", str(self.new / "scripts/migrate-v1.sh")], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((self.old / "dispatcher.py").read_text(), "v1", "v1 untouched")
        self.assertTrue(self.new.exists())
        self.assertNotIn("stop telegram-agent", self.log.read_text() if self.log.exists() else "")

    def test_bot_that_never_pauses_leaves_v1_running(self):
        self.env["TG_MIGRATE_PAUSE_WAIT"] = "1"  # and no bot thread acknowledges the pause
        timer_active = STUB.replace("*is-active*telegram-agent*)", "*is-active*telegram-agent*|*is-active*timer*)")
        (Path(self.tmp.name) / "bin/systemctl").write_text(timer_active)
        r = subprocess.run(["bash", str(self.new / "scripts/migrate-v1.sh")], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restarting v1", r.stderr)
        self.assertFalse((self.old / "state/pause").exists())
        calls = self.log.read_text()
        self.assertIn("--user stop telegram-mcp-update.timer", calls)
        self.assertIn("--user start telegram-mcp-update.timer", calls)
        self.assertNotIn("stop telegram-agent", calls)
        self.assertTrue((self.old / "dispatcher.py").exists() and self.new.exists())

    def test_refuses_without_v1(self):
        shutil.rmtree(self.old)
        r = subprocess.run(["bash", str(self.new / "scripts/migrate-v1.sh")], env=self.env, capture_output=True,
                           text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no v1 install", r.stderr)


if __name__ == "__main__":
    unittest.main()
