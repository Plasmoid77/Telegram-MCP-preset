"""Tests for the Claude Code hooks guard.py and reflow.py (stdlib only).

Run: python3 -m unittest discover -s tests -v
"""
import json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path

AGENT = (Path(__file__).resolve().parent.parent / "agent").resolve()
GUARD, REFLOW = AGENT / "guard.py", AGENT / "reflow.py"


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.config = t / "claude"
        self.results = self.config / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(AGENT))
        self.files = t / "TelegramMCP"
        for d in (self.results / "sid/tool-results", self.files):
            d.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, script, event, tier="guest", chat="100", notify="900"):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.config), TG_FILES_DIR=str(self.files),
                   TG_TIER=tier, TG_CHAT_ID=chat, TG_NOTIFY=notify, TG_SELF_ID="555")
        return subprocess.run([sys.executable, "-I", str(script)], input=json.dumps(event),
                              capture_output=True, text=True, env=env)


class Guard(Env):
    def check(self, tool, args, tier="guest", allowed=True):
        r = self.run_hook(GUARD, {"tool_name": tool, "tool_input": args}, tier)
        self.assertEqual(r.returncode, 0 if allowed else 2, f"{tier} {tool} {args}: {r.stderr}")
        if not allowed:
            self.assertIn("Blocked by guard", r.stderr)

    def test_owner_read(self):
        self.check("Read", {"file_path": str(self.results / "sid/tool-results/x.txt")}, "owner")
        self.check("Read", {"file_path": str(self.files / "export.json")}, "owner")
        self.check("Read", {"file_path": str(AGENT / "memory.md")}, "owner")

    def test_owner_read_blocked(self):
        for p in ("/etc/passwd", str(AGENT / "config.env"), str(AGENT / "prompt.md"),
                  str(self.files) + "2/x",                     # look-alike prefix
                  str(self.files / "../claude/../../etc/passwd")):  # traversal
            self.check("Read", {"file_path": p}, "owner", allowed=False)

    def test_owner_write_only_memory(self):
        self.check("Edit", {"file_path": str(AGENT / "memory.md")}, "owner")
        self.check("Write", {"file_path": str(AGENT / "memory.md")}, "owner")
        for p in (AGENT / "prompt.md", AGENT / "guard.py", AGENT / "memory.md.bak"):
            self.check("Edit", {"file_path": str(p)}, "owner", allowed=False)
            self.check("Write", {"file_path": str(p)}, "owner", allowed=False)

    def test_guest_has_no_files(self):
        for tool in ("Read", "Edit", "Write"):
            self.check(tool, {"file_path": str(AGENT / "memory.md")}, "guest", allowed=False)

    def test_owner_telegram_unrestricted(self):
        self.check("mcp__telegram__send_message", {"chat_id": "777", "message": "hi"}, "owner")
        self.check("mcp__telegram__get_history", {"chat_id": "777"}, "owner")

    def test_guest_current_chat_only(self):
        self.check("mcp__telegram__get_me", {})
        self.check("mcp__telegram__get_history", {"chat_id": "100"})
        self.check("mcp__telegram__send_message", {"chat_id": 100, "message": "hi"})
        self.check("mcp__telegram__send_message", {"chat_id": "900", "message": "to owner"})
        self.check("mcp__telegram__get_history", {"chat_id": "777"}, allowed=False)
        self.check("mcp__telegram__send_message", {"chat_id": "777", "message": "x"}, allowed=False)
        self.check("mcp__telegram__get_user_status", {"user_id": "777"}, allowed=False)

    def test_guest_transcribe_local_engine_only(self):
        self.check("mcp__telegram__transcribe_voice", {"chat_id": "100", "message_id": 5})
        self.check("mcp__telegram__transcribe_voice", {"chat_id": "100", "message_id": 5, "engine": "openai"})
        for engine in ("groq", "telegram", "whisper"):
            self.check("mcp__telegram__transcribe_voice", {"chat_id": "100", "message_id": 5, "engine": engine},
                       allowed=False)
        self.check("mcp__telegram__transcribe_voice", {"chat_id": "777", "message_id": 5}, allowed=False)
        self.check("mcp__telegram__transcribe_voice", {"chat_id": "777", "engine": "groq"}, "owner")

    def test_guest_forward(self):
        self.check("mcp__telegram__forward_message", {"from_chat_id": "100", "to_chat_id": "900", "message_id": 1})
        self.check("mcp__telegram__forward_message", {"from_chat_id": "777", "to_chat_id": "900", "message_id": 1},
                   allowed=False)
        self.check("mcp__telegram__forward_message", {"from_chat_id": "100", "to_chat_id": "777", "message_id": 1},
                   allowed=False)


class Reflow(Env):
    @staticmethod
    def payload(n):
        inner = {"chat_id": 1, "messages": [{"id": i, "text": "x" * 100} for i in range(n)]}
        return json.dumps({"result": json.dumps(inner)})

    def test_small_result_untouched(self):
        r = self.run_hook(REFLOW, {"tool_response": self.payload(3)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_saved_file_is_reflowed(self):
        src = self.results / "sid/tool-results/big.txt"
        src.write_text(self.payload(5))
        r = self.run_hook(REFLOW, {"tool_response": f"Output too large, saved to {src}. Use Read."})
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        lines = (self.results / "sid/tool-results/big.lines.txt").read_text().splitlines()
        self.assertEqual(len(lines), 2 + 5)  # header + "# messages: 5 records" + 5 records
        self.assertEqual(json.loads(lines[2])["id"], 0)
        self.assertIn("big.lines.txt", out["updatedMCPToolOutput"])

    def test_file_outside_project_ignored(self):
        src = self.files / "big.txt"
        src.write_text(self.payload(5))
        r = self.run_hook(REFLOW, {"tool_response": f"saved to {src}"})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertFalse((self.files / "big.lines.txt").exists())

    def test_big_inline_result_reflowed(self):
        r = self.run_hook(REFLOW, {"tool_response": self.payload(300)})
        text = json.loads(r.stdout)["hookSpecificOutput"]["updatedMCPToolOutput"]
        self.assertTrue(text.startswith("# 300 "))
        self.assertEqual(len(text.splitlines()), 1 + 2 + 300)


class Config(unittest.TestCase):
    def test_json_files(self):
        for f in ("settings.json", "mcp.json"):
            json.loads((AGENT / f).read_text())

    def test_prompt_placeholders_are_configured(self):
        keys = set(re.findall(r"\{\{(\w+)\}\}", (AGENT / "prompt.md").read_text()))
        conf = (AGENT / "config.env.example").read_text()
        defined = set(re.findall(r"^#?([A-Z_]+)=", conf, re.M))
        self.assertLessEqual(keys, defined)


if __name__ == "__main__":
    unittest.main()
