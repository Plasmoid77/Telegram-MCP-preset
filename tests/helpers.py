"""Shared test helpers: import agent modules, a fake telegram-mcp upstream."""
import json
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "agent"
if str(AGENT) not in sys.path:
    sys.path.insert(0, str(AGENT))

UPSTREAM_TOOLS = [
    "get_me", "get_history", "get_messages_after", "get_message_context", "get_messages", "list_messages",
    "search_messages", "list_topics", "get_message_link", "get_chat", "get_user_status", "get_media_info",
    "open_photo", "inspect_document", "transcribe_voice", "send_message", "reply_to_message", "send_reaction",
    "mark_as_read", "send_typing", "forward_message", "list_chats", "search_global", "resolve_username",
    "list_folders", "get_folder", "create_folder", "delete_folder", "add_chat_to_folder",
    "remove_chat_from_folder", "send_file", "export_chat_history", "edit_message", "delete_message",
    "leave_chat", "update_profile", "disable_incoming_feed", "wait_for_events", "wait_for_settled_message",
]


class FakeUpstream:
    """Records tool calls; answers with canned text."""

    def __init__(self, tools=UPSTREAM_TOOLS, answers=None):
        self.tools, self.calls, self.answers = tools, [], answers or {}

    def catalog(self):
        return [{"name": n, "description": n, "inputSchema": {"type": "object"}} for n in self.tools]

    def tool_result(self, name, args, timeout=60):
        self.calls.append((name, args))
        answer = self.answers.get(name, "Message sent successfully. message_id=7" if name == "send_message" else "{}")
        if callable(answer):
            answer = answer(args)
        if isinstance(answer, dict) and "content" in answer:
            return answer
        return {"content": [{"type": "text", "text": answer if isinstance(answer, str) else json.dumps(answer)}],
                "isError": False}

    def call(self, name, args, timeout=60, raw=False):
        text = self.tool_result(name, args, timeout)["content"][0]["text"]
        return text if raw else json.loads(text)


CFG = {"OWNER_ID": "100", "SELF_ID": "200", "OWNER_NAME": "Иван", "OWNER_USERNAME": "@owner_x",
       "NOTIFY_CHAT": "", "WEB_SEARCH": "duckduckgo", "PAGE_CHARS": "30000", "BOT_NAME": "Бот",
       "ACCOUNT_NAME": "Аккаунт", "GROUP_TRIGGERS": "бот", "GROUPS_FOLDER": "Папка", "FILES_DIR": "~/x"}
