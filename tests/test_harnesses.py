"""harnesses.py: every adapter gets only the broker URL; instructions never on the command line."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
import harnesses  # noqa: E402

URL = "http://127.0.0.1:5555/mcp/secret"
CFG = {"MODEL": "m", "MAX_TURNS": "15", "RUN_TIMEOUT": "300", "OWNER_MAX_TURNS": "40", "OWNER_RUN_TIMEOUT": "900"}


def build(harness, cfg=None, retry=False):
    cfg = dict(CFG, HARNESS=harness, **(cfg or {}))
    with tempfile.TemporaryDirectory() as d:
        env = {}
        argv, stdin = harnesses.command(cfg, harness, Path(d), URL, ["send_message"], 15,
                                        "SECRET INSTRUCTIONS", "TASK TEXT", env, retry)
        files = {p.name: p.read_text() for p in Path(d).iterdir() if p.is_file()}
        return argv, stdin, env, files


class Adapters(unittest.TestCase):
    def test_claude_broker_only_and_no_ambient_config(self):
        argv, stdin, env, files = build("claude")
        self.assertEqual(json.loads(files["mcp.json"]), {"mcpServers": {"telegram": {"type": "http", "url": URL}}})
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "mcp__telegram")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"autoMemoryEnabled": False})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(files["system.md"], "SECRET INSTRUCTIONS")
        self.assertEqual(stdin, "TASK TEXT")

    def test_no_adapter_puts_the_prompt_on_the_command_line(self):
        for h in ("claude", "codex", "opencode"):
            argv, stdin, _, _ = build(h)
            with self.subTest(h):
                self.assertFalse(any("SECRET" in a or "TASK" in a for a in argv))
                self.assertIn("TASK TEXT", stdin)

    def test_codex_has_no_shell_and_only_the_broker(self):
        argv, _, _, _ = build("codex")
        self.assertIn("--ignore-user-config", argv)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        for feature in ("shell_tool", "unified_exec"):
            self.assertIn(feature, argv)
        self.assertIn(f"mcp_servers.telegram.url={json.dumps(URL)}", argv)
        self.assertIn('mcp_servers.telegram.enabled_tools=["send_message"]', argv)

    def test_opencode_denies_everything_but_the_broker(self):
        argv, _, env, files = build("opencode", {"MODEL": "opencode-go/qwen3.7-plus"})
        conf = json.loads(files["opencode.json"])
        self.assertEqual(conf["mcp"]["servers"]["telegram"]["url"], URL)
        self.assertEqual(conf["permissions"][0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertEqual(conf["providers"]["opencode-go"]["settings"]["headerTimeout"], 20000)
        self.assertEqual(env["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        _, _, _, files = build("opencode", {"MODEL": "opencode-go/x"}, retry=True)
        self.assertEqual(json.loads(files["opencode.json"])["providers"]["opencode-go"]["settings"]["headerTimeout"],
                         harnesses.OPENCODE_RETRY_HEADER_TIMEOUT_MS)

    def test_command_gets_url_in_env(self):
        argv, stdin, env, _ = build("command", {"HARNESS_COMMAND": "/bin/runner --x"})
        self.assertEqual(argv, ["/bin/runner", "--x"])
        self.assertEqual(env["TG_MCP_URL"], URL)


class Outcome(unittest.TestCase):
    def test_claude(self):
        self.assertTrue(harnesses.outcome("claude", 0, json.dumps({"is_error": False})).ok)
        self.assertFalse(harnesses.outcome("claude", 0, json.dumps({"is_error": True})).ok)
        self.assertTrue(harnesses.outcome("claude", 1, "garbage").transient)

    def test_header_timeout_wording_of_v1_is_transient(self):
        r = harnesses.outcome("opencode", 1, '{"type":"error","error":{"message":"timed out waiting for response headers"}}')
        self.assertTrue(r.transient)

    def test_codex_and_opencode(self):
        self.assertTrue(harnesses.outcome("codex", 0, '{"type":"turn.completed"}').ok)
        self.assertFalse(harnesses.outcome("codex", 0, '{"type":"turn.completed"}\n{"type":"error"}').ok)
        r = harnesses.outcome("opencode", 1, '{"type":"error","error":{"message":"Headers Timeout"}}')
        self.assertEqual((r.ok, r.transient), (False, True))
        self.assertTrue(harnesses.outcome("opencode", 0, '{"type":"step_finish"}').ok)


class Run(unittest.TestCase):
    def fake_cli(self, d, body):
        p = Path(d) / "fake"
        p.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def test_prompt_on_stdin_and_success(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "seen"
            cli = self.fake_cli(d, f"""
                import sys, os
                open({str(out)!r}, "w").write(sys.stdin.read() + "|" + os.environ["TG_MCP_URL"])
            """)
            cfg = dict(CFG, HARNESS="command", HARNESS_COMMAND=cli)
            r = harnesses.run(cfg, False, "INSTR", "TASK", URL, ["x"], timeout=20)
            self.assertTrue(r.ok)
            self.assertEqual(out.read_text(), "INSTR\n\n# Задача\nTASK|" + URL)

    def test_stderr_tail_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            cli = self.fake_cli(d, """
                import sys
                sys.stderr.write("x" * 5000 + "Error: not logged in")
                sys.exit(1)
            """)
            r = harnesses.run(dict(CFG, HARNESS="command", HARNESS_COMMAND=cli), False, "I", "T", URL, [], timeout=20)
            self.assertFalse(r.ok)
            self.assertTrue(r.stderr.endswith("Error: not logged in"))
            self.assertLessEqual(len(r.stderr), 2000)

    def test_timeout_kills_the_process_group(self):
        with tempfile.TemporaryDirectory() as d:
            cli = self.fake_cli(d, """
                import subprocess, time
                subprocess.Popen(["sleep", "30"])
                time.sleep(30)
            """)
            cfg = dict(CFG, HARNESS="command", HARNESS_COMMAND=cli)
            with self.assertRaises(subprocess.TimeoutExpired):
                harnesses.run(cfg, False, "I", "T", URL, [], timeout=1)

    def test_preflight(self):
        good = dict(CFG, HARNESS="command", HARNESS_COMMAND=sys.executable, OWNER_ID="1", SELF_ID="2")
        harnesses.preflight(good)
        for bad in ({"OWNER_ID": "<owner_user_id>"}, {"HARNESS": "nope"}, {"MAX_TURNS": "0"},
                    {"HARNESS_COMMAND": "/nonexistent/x"},
                    {"HARNESS": "opencode", "OPENCODE_BIN": sys.executable, "OPENCODE_HEADER_TIMEOUT_MS": "20s"}):
            with self.subTest(bad), self.assertRaises(ValueError):
                harnesses.preflight(dict(good, **bad))


if __name__ == "__main__":
    unittest.main()
