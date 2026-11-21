"""Paths and settings of telegram-agent.

Code lives in the git checkout (this directory's parent). The owner's settings live in
~/.config/telegram-agent/, runtime state and the bot's memory in ~/.local/state/telegram-agent/
(XDG_CONFIG_HOME / XDG_STATE_HOME are honoured).
"""
import os
from pathlib import Path

CODE = Path(__file__).resolve().parent
PROMPTS = CODE / "prompts"


def _xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default)


# The units pass the directories install.sh chose, so the service and your shell always agree.
CONFIG_DIR = Path(os.environ.get("TG_AGENT_CONFIG_DIR") or _xdg("XDG_CONFIG_HOME", ".config") / "telegram-agent")
STATE_DIR = Path(os.environ.get("TG_AGENT_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state") / "telegram-agent")
CONFIG = CONFIG_DIR / "config.env"
MEMORY = STATE_DIR / "memory.md"
CURSORS = STATE_DIR / "cursors.json"
PAUSE = STATE_DIR / "pause"          # update_mcp.py: start no new runs while it exists
PAUSE_ACK = STATE_DIR / "pause.ack"  # written by the dispatcher while paused and idle

DEFAULTS = {
    "MCP_URL": "http://127.0.0.1:8765/mcp",
    "HARNESS": "claude",
    "MODEL": "",
    "MAX_TURNS": "15",
    "RUN_TIMEOUT": "300",
    "OWNER_MAX_TURNS": "40",
    "OWNER_RUN_TIMEOUT": "900",
    "PER_CHAT_PER_HOUR": "20",
    "TOTAL_PER_HOUR": "80",
    "OWNER_PER_HOUR": "60",
    "MAX_AGE": "1800",
    "RECONCILE_INTERVAL": "180",
    "RECONCILE_CHATS": "50",
    "FILES_DIR": "~/TelegramMCP",
    "NOTIFY_CHAT": "",
    "GROUPS": "",
    "GROUPS_FOLDER": "",
    "GROUP_TRIGGERS": "",
    "WEB_SEARCH": "duckduckgo",
    "PAGE_CHARS": "30000",
}


def load(path=CONFIG):
    """config.env as a dict over DEFAULTS. Re-read on every loop: edits apply without a restart."""
    cfg = dict(DEFAULTS)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    return cfg


def items(cfg, key):
    """A comma-separated setting as a list of non-empty strings."""
    return [x.strip() for x in cfg.get(key, "").split(",") if x.strip()]


def integer(cfg, key):
    value = int(cfg[key])
    if value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value
