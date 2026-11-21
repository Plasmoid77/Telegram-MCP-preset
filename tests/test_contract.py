"""Every tool and argument the preset uses exists in the pinned telegram-mcp release.

Reads the upstream source (no dependencies, nothing runs): set UPSTREAM_SRC to a checkout of
the tag install.sh pins (CI does). Skipped without it.
"""
import ast
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
import policy  # noqa: E402

# Tools the dispatcher, broker and updater call themselves, with the arguments they pass.
OWN_CALLS = {
    "get_messages_after": {"chat_id", "after_id", "limit"},
    "get_message_context": {"chat_id", "message_id", "context_size"},
    "list_chats": {"chat_type", "limit"},
    "get_chat": {"chat_id"},
    "wait_for_events": {"after_seq", "boot", "timeout"},
    "list_folders": set(),
    "get_folder": {"folder_id"},
    "send_message": {"chat_id", "message"},
    "forward_message": {"from_chat_id", "message_id", "to_chat_id"},
    "get_me": set(),
}


def upstream_tools(root):
    tools = {}
    for path in (Path(root) / "telegram_mcp").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                    (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool")
                    or getattr(d, "attr", "") == "tool" for d in node.decorator_list):
                tools[node.name] = {a.arg for a in node.args.args + node.args.kwonlyargs}
    return tools


@unittest.skipUnless(os.environ.get("UPSTREAM_SRC"), "set UPSTREAM_SRC to a telegram-mcp checkout")
class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = upstream_tools(os.environ["UPSTREAM_SRC"])

    def test_parser_sees_the_server(self):
        self.assertGreater(len(self.tools), 100)

    def test_chat_scope_arguments_exist(self):
        for name, (allowed, caps) in policy.CHAT_TOOLS.items():
            if name in policy.OPTIONAL_TOOLS and name not in self.tools:
                continue
            with self.subTest(name):
                self.assertIn(name, self.tools)
                self.assertLessEqual(allowed | set(caps), self.tools[name])

    def test_owner_and_blocked_names_exist(self):
        for name in (policy.OWNER_DIRECT | policy.BLOCKED) - policy.OPTIONAL_TOOLS:
            with self.subTest(name):
                self.assertIn(name, self.tools)

    def test_own_calls(self):
        for name, args in OWN_CALLS.items():
            with self.subTest(name):
                self.assertLessEqual(args, self.tools[name])


if __name__ == "__main__":
    unittest.main()
