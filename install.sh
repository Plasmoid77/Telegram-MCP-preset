#!/usr/bin/env bash
# Telegram-MCP-preset installer, for the current user (no root needed).
#
# 1. telegram-mcp from the upstream repo (https://github.com/chigwell/telegram-mcp), pinned tag
# 2. the telegram-agent auto-responder from this repo
# 3. asr-server: local speech-to-text for voice messages (onnx-asr, default GigaAM-v3)
# 4. the systemd --user units
#
# Safe to re-run. It never touches secrets (.env, *.session, backend.env), config.env,
# prompt.md or memory.md once they exist, and it never starts, stops or restarts services.
# Replaced files are backed up to ~/.local/share/telegram-agent/state/*.bak-<time>.
set -euo pipefail

UPSTREAM=https://github.com/chigwell/telegram-mcp.git
REF=${TELEGRAM_MCP_REF:-v3.3.23}          # the tag this preset was verified with
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MCP_DIR=$HOME/.local/share/telegram-mcp
AGENT_DIR=$HOME/.local/share/telegram-agent
ASR_DIR=$HOME/.local/share/asr-server
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
FILES_DIR=$HOME/TelegramMCP
STAMP=$(date +%Y%m%d-%H%M%S)

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found: $2"; }

# install SRC DST MODE: copy, backing up a different existing file first
put() {
    if [[ -e $2 ]] && cmp -s "$1" "$2"; then return 0; fi
    if [[ -e $2 ]]; then
        cp -p "$2" "$AGENT_DIR/state/$(basename "$2").bak-$STAMP"
        say "updated $2 (old copy in state/$(basename "$2").bak-$STAMP)"
    else
        say "installed $2"
    fi
    install -m "$3" "$1" "$2"
}

# install SRC DST MODE only if DST does not exist yet
keep() {
    if [[ -e $2 ]]; then
        cmp -s "$1" "$2" || say "kept your $2 (template: $1)"
        return 0
    fi
    install -m "$3" "$1" "$2"
    say "created $2 from the template"
}

need git "https://git-scm.com/"
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need python3 "Python 3.10+ is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10+ is required"

# --- 1. telegram-mcp (upstream, unmodified) ---------------------------------------------------
if [[ ! -d $MCP_DIR/.git ]]; then
    say "cloning $UPSTREAM ($REF) into $MCP_DIR"
    git clone --quiet "$UPSTREAM" "$MCP_DIR"
    git -C "$MCP_DIR" -c advice.detachedHead=false checkout --quiet "$REF"
else
    say "telegram-mcp already in $MCP_DIR at $(git -C "$MCP_DIR" describe --tags --always) (upgrades are manual, docs/USAGE.md)"
fi
(cd "$MCP_DIR" && uv sync --quiet)
if [[ ! -e $MCP_DIR/.env ]]; then
    install -m 600 "$SRC/server/env.example" "$MCP_DIR/.env"
    say "created $MCP_DIR/.env (600): fill in TELEGRAM_API_ID / TELEGRAM_API_HASH yourself, then log in (docs/INSTALL.md, step 4)"
fi
install -d -m 700 "$FILES_DIR"

# --- 2. telegram-agent ----------------------------------------------------------------------
mkdir -p "$AGENT_DIR"
install -d -m 700 "$AGENT_DIR/state"
for f in dispatcher.py guard.py reflow.py settings.json mcp.json; do
    put "$SRC/agent/$f" "$AGENT_DIR/$f" 644
done
keep "$SRC/agent/prompt.md" "$AGENT_DIR/prompt.md" 644
keep "$SRC/agent/config.env.example" "$AGENT_DIR/config.env" 600
keep "$SRC/agent/backend.env.example" "$AGENT_DIR/backend.env" 600

# --- 3. asr-server (voice messages) ---------------------------------------------------------
mkdir -p "$ASR_DIR"
for f in asr_server.py pyproject.toml uv.lock; do
    put "$SRC/asr/$f" "$ASR_DIR/$f" 644
done
(cd "$ASR_DIR" && uv sync --quiet --frozen)
command -v ffmpeg >/dev/null 2>&1 || say "ffmpeg not found: install it, asr-server needs it to decode voice messages"

# --- 4. systemd --user units ------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
for u in telegram-mcp.service telegram-agent.service asr-server.service; do
    put "$SRC/systemd/$u" "$UNIT_DIR/$u" 644
done
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
fi

# --- What is left ---------------------------------------------------------------------------
command -v claude >/dev/null 2>&1 || [[ -x $HOME/.local/bin/claude ]] \
    || say "Claude Code not found: install it (https://code.claude.com/docs/en/setup) and log in"
grep -q '<' "$AGENT_DIR/config.env" && say "config.env still has <placeholders>: fill them in"
cat <<EOF
==> Next (docs/INSTALL.md):
    - secrets in $MCP_DIR/.env and Telegram login (step 4), in your own terminal
    - $AGENT_DIR/config.env (step 6)
    - systemctl --user enable --now asr-server telegram-mcp telegram-agent
    - loginctl enable-linger \$USER   (keep running without a login session)
EOF
