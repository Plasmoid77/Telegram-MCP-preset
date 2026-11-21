"""Which Telegram tool a run may call, with which arguments.

Two scopes:
- "owner": the owner's private chat with the bot account. The whole account is available.
- "chat": a guest's private chat, or any group run (even one the owner started). Only the
  current chat: read it, answer in it, look at its media; notify the owner. Text written by
  other people can steer a model, so a run that reads it never gets account-wide tools.

Every function here is pure; broker.py applies them to each call before it reaches telegram-mcp.
"""
import json

# Never offered to any run: they control the server's own update stream.
BLOCKED = {"disable_incoming_feed", "enable_incoming_feed", "incoming_feed_status",
           "wait_for_settled_message", "wait_for_events"}

# Tools of the current chat: allowed arguments and upper bounds for numeric ones.
# chat_id is always pinned to the current chat.
CHAT_TOOLS = {
    "get_history": ({"chat_id", "limit", "topic_id"}, {"limit": 100}),
    "get_messages_after": ({"chat_id", "after_id", "limit"}, {"limit": 100}),
    "get_message_context": ({"chat_id", "message_id", "context_size"}, {"context_size": 30}),
    "get_messages": ({"chat_id", "page", "page_size"}, {"page_size": 50}),
    "list_messages": ({"chat_id", "limit", "search_query", "from_date", "to_date"}, {"limit": 100}),
    "search_messages": ({"chat_id", "query", "limit"}, {"limit": 50}),
    "list_topics": ({"chat_id", "limit", "offset_topic", "search_query"}, {"limit": 100}),
    "get_message_link": ({"chat_id", "message_id", "thread"}, {}),
    "get_chat": ({"chat_id"}, {}),
    "get_media_info": ({"chat_id", "message_id"}, {}),
    "open_photo": ({"chat_id", "photo_id", "message_id"}, {}),
    "inspect_document": ({"chat_id", "message_id"}, {}),
    "transcribe_voice": ({"chat_id", "message_id"}, {}),
    "view_video": ({"chat_id", "message_id", "start", "end", "max_frames", "transcribe"}, {"max_frames": 24}),
    "send_message": ({"chat_id", "message", "parse_mode", "reply_to", "silent"}, {}),
    "reply_to_message": ({"chat_id", "message_id", "text", "parse_mode", "silent"}, {}),
    "send_reaction": ({"chat_id", "message_id", "emoji", "big"}, {}),
    "mark_as_read": ({"chat_id"}, {}),
    "send_typing": ({"chat_id", "seconds", "topic_id"}, {"seconds": 30}),
}
# Tools whose upstream release may not have them yet (offered only when the server does).
OPTIONAL_TOOLS = {"view_video"}
READS = {"get_history", "get_messages_after", "get_message_context", "get_messages", "list_messages",
         "search_messages", "list_topics", "get_message_link", "get_chat", "get_media_info",
         "open_photo", "inspect_document", "transcribe_voice", "view_video", "get_me",
         "get_user_status", "list_chats", "search_global", "resolve_username", "list_folders",
         "get_folder"}
STATUS = {"mark_as_read", "send_typing", "send_reaction"}
SENDS = {"send_message", "reply_to_message"}

# Offered directly in owner runs; the rest of the server's tools via find/call_telegram_tool.
OWNER_DIRECT = set(CHAT_TOOLS) | {
    "get_me", "get_user_status", "forward_message", "list_chats", "search_global",
    "resolve_username", "list_folders", "get_folder", "create_folder", "delete_folder",
    "add_chat_to_folder", "remove_chat_from_folder", "send_file", "export_chat_history",
    "edit_message", "delete_message",
}


def delivered(result):
    """True when a send's MCP result is a confirmed delivery (upstream may report a failure as
    ordinary text in a successful envelope)."""
    if result.get("isError"):
        return False
    text = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
    if text.startswith(("Message sent", "Replied to message")):
        return True
    try:
        data = json.loads(text)
    except ValueError:
        return False
    return isinstance(data, dict) and (data.get("sent") is True or isinstance(data.get("message_id"), int))


def chat_ref(value):
    """A chat reference as an int when it is a plain numeric id, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdecimal():
            return int(text)
    return None


def notify_target(cfg):
    """Where notify_owner summaries go: NOTIFY_CHAT (an id, or "me" = the bot account's Saved
    Messages), else the owner's id."""
    value = (cfg.get("NOTIFY_CHAT") or "").strip().lower()
    if value in ("me", "self"):
        return chat_ref(cfg.get("SELF_ID"))
    return chat_ref(value) or chat_ref(cfg.get("OWNER_ID"))


def offered(scope, upstream_names):
    """Upstream tool names a run of this scope sees directly."""
    names = set(upstream_names) - BLOCKED
    if scope == "owner":
        return names & OWNER_DIRECT
    return names & (set(CHAT_TOOLS) | {"get_me", "get_user_status"})


def check(scope, name, args, chat, private):
    """Arguments to forward upstream, or None when the call is not allowed.

    scope: "owner" or "chat"; chat: current chat id; private: the current chat is a private
    chat (not a group). Chat-scope runs reach the owner only through the broker's notify_owner.
    """
    if name in BLOCKED or not isinstance(args, dict):
        return None
    if scope == "owner":
        return args
    if args.get("account") not in (None, ""):
        return None
    args = {k: v for k, v in args.items() if k != "account"}
    if name == "get_me":
        return {}
    if name == "get_user_status":
        return {"user_id": chat} if private and chat_ref(args.get("user_id")) == chat and set(args) == {"user_id"} else None
    rule = CHAT_TOOLS.get(name)
    if rule is None:
        return None
    allowed, caps = rule
    if not set(args) <= allowed or chat_ref(args.get("chat_id")) != chat:
        return None
    args = dict(args, chat_id=chat)
    for key, cap in caps.items():
        value = args.get(key)
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return None
            args[key] = min(value, cap)
    return args


def stays_in_chat(name, args, chat):
    """True when an owner-run call only touches the current chat (it keeps web search open)."""
    return (name in CHAT_TOOLS and chat_ref(args.get("chat_id")) == chat
            and args.get("account") in (None, ""))
