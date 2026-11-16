"""PreToolUse guard for the Telegram auto-responder.

In "guest" tier (a stranger's chat) the agent may read only the current chat and
may write only to the current chat or to the owner-notification chat.
Owner tier is unrestricted in Telegram; its file access is limited to Read of saved tool
results, FILES_DIR and memory.md, and Edit/Write of memory.md only.
Exit code 2 blocks the call and tells the model why.
"""
import json, os, re, sys

BASE = os.path.dirname(os.path.realpath(__file__))
MEMORY = os.path.realpath(os.path.join(BASE, "memory.md"))
# Where Claude Code keeps this bot's runs (incl. tool-results/): <config dir>/projects/<cwd with
# every non-alphanumeric character replaced by "-">. The dispatcher runs claude with cwd = BASE.
RESULTS = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                       "projects", re.sub(r"[^A-Za-z0-9]", "-", BASE))
FILES = os.path.expanduser(os.environ.get("TG_FILES_DIR") or "~/TelegramMCP")

ev = json.load(sys.stdin)
tier = os.environ.get("TG_TIER", "guest")

# Read (owner runs only get this tool): only large tool results Claude Code saved for this
# bot, and the TelegramMCP folder (exports). Never secrets, configs or the owner's files.
if ev.get("tool_name") == "Read":
    path = os.path.realpath(str((ev.get("tool_input") or {}).get("file_path", "")))
    roots = [os.path.realpath(r) for r in (RESULTS, FILES)]
    if tier == "owner" and (path == MEMORY or any(path == r or path.startswith(r + os.sep) for r in roots)):
        sys.exit(0)
    print(f"Blocked by guard: Read is only allowed for saved tool results, {FILES} and your memory file.",
          file=sys.stderr)
    sys.exit(2)

# Edit/Write (owner runs only): the bot's own memory file and nothing else.
if ev.get("tool_name") in ("Edit", "Write"):
    path = os.path.realpath(str((ev.get("tool_input") or {}).get("file_path", "")))
    if tier == "owner" and path == MEMORY:
        sys.exit(0)
    print("Blocked by guard: you may only edit your memory file " + MEMORY + ".", file=sys.stderr)
    sys.exit(2)

if tier == "owner":
    sys.exit(0)
tool = ev.get("tool_name", "").removeprefix("mcp__telegram__")
args = ev.get("tool_input") or {}
cur = os.environ.get("TG_CHAT_ID", "")
notify = {os.environ.get("TG_NOTIFY", "me")}
if notify & {"me", "self", os.environ.get("TG_SELF_ID")}:
    notify |= {"me", "self", os.environ.get("TG_SELF_ID", "me")}

def norm(v):
    return str(v).strip().lower().lstrip("@")

def deny(msg):
    print(f"Blocked by guard: {msg}", file=sys.stderr)
    sys.exit(2)

if tool == "get_me":
    sys.exit(0)
if tool in ("send_message", "reply_to_message"):
    if norm(args.get("chat_id")) not in {norm(cur)} | {norm(n) for n in notify}:
        deny("you may only write to the current chat or notify the owner.")
    sys.exit(0)
if tool == "forward_message":
    if norm(args.get("from_chat_id")) != norm(cur):
        deny("you may only forward messages from the current chat.")
    if norm(args.get("to_chat_id")) not in {norm(n) for n in notify}:
        deny("you may only forward to the owner.")
    sys.exit(0)
for key in ("chat_id", "user_id"):
    if key in args and norm(args[key]) != norm(cur):
        deny("in this conversation you may only access the current chat.")
sys.exit(0)
