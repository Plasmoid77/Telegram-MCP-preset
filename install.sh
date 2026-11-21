#!/usr/bin/env bash
# Telegram-MCP-preset installer, for the current user (no root needed). Safe to re-run.
#
# Run it from the git checkout that will stay in place (by default ~/.local/share/telegram-agent):
# the units point at this checkout, so upgrading = git checkout of a newer tag + restart.
#
# 1. telegram-mcp from the upstream repo (https://github.com/chigwell/telegram-mcp), pinned tag
# 2. your settings in ~/.config/telegram-agent (created from templates once, never overwritten)
# 3. asr-server's Python environment (local speech-to-text, onnx-asr)
# 4. the systemd --user units
#
# It never touches secrets once they exist, and never starts, stops or restarts services.
set -euo pipefail

UPSTREAM=https://github.com/chigwell/telegram-mcp.git
REF=${TELEGRAM_MCP_REF:-v3.3.23}          # the tag this preset was verified with
SRC=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MCP_DIR=$HOME/.local/share/telegram-mcp
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/telegram-agent
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/telegram-agent
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
FILES_DIR=$HOME/TelegramMCP
V1_DIR=$HOME/.local/share/telegram-agent

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found: $2"; }

need git "https://git-scm.com/"
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need python3 "Python 3.10+ is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 3.10+ is required"
for p in "$SRC" "$CONFIG_DIR" "$STATE_DIR"; do
    [[ $p != *[[:space:]]* ]] || die "paths with spaces are not supported by the units: $p"
done
if [[ -f $V1_DIR/config.env && ! -e $V1_DIR/.git && $SRC != "$V1_DIR" ]]; then
    die "a v1 install is in $V1_DIR: migrate it with $SRC/scripts/migrate-v1.sh (docs/INSTALL.md)"
fi

# --- 1. telegram-mcp (upstream, unmodified) ---------------------------------------------------
if [[ ! -d $MCP_DIR/.git ]]; then
    say "cloning $UPSTREAM ($REF) into $MCP_DIR"
    git clone --quiet "$UPSTREAM" "$MCP_DIR"
    git -C "$MCP_DIR" -c advice.detachedHead=false checkout --quiet "$REF"
else
    at=$(git -C "$MCP_DIR" describe --tags --exact-match 2>/dev/null || echo "untagged $(git -C "$MCP_DIR" rev-parse --short HEAD)")
    say "telegram-mcp already in $MCP_DIR at $at (verified with $REF; upgrade: agent/update_mcp.py apply)"
fi
(cd "$MCP_DIR" && uv sync --quiet)
if [[ ! -e $MCP_DIR/.env ]]; then
    install -m 600 "$SRC/server/env.example" "$MCP_DIR/.env"
    say "created $MCP_DIR/.env (600): fill in TELEGRAM_API_ID / TELEGRAM_API_HASH yourself (docs/INSTALL.md)"
fi
install -d -m 700 "$FILES_DIR"

# --- 2. settings and state --------------------------------------------------------------------
install -d -m 700 "$CONFIG_DIR" "$STATE_DIR"
for pair in config.env.example:config.env backend.env.example:backend.env; do
    if [[ ! -e $CONFIG_DIR/${pair#*:} ]]; then
        install -m 600 "$SRC/agent/${pair%%:*}" "$CONFIG_DIR/${pair#*:}"
        say "created $CONFIG_DIR/${pair#*:} from the template"
    fi
done

# --- 3. asr-server (voice messages) -----------------------------------------------------------
(cd "$SRC/asr" && uv sync --quiet --frozen)
mkdir -p "$HOME/.cache/huggingface"
command -v ffmpeg >/dev/null 2>&1 || say "ffmpeg not found: install it, asr-server needs it to decode voice messages"

# --- 4. systemd --user units (pointing at this checkout) --------------------------------------
mkdir -p "$UNIT_DIR"
for u in telegram-mcp.service telegram-agent.service asr-server.service \
         telegram-mcp-update.service telegram-mcp-update.timer; do
    tmp=$(mktemp)
    sed -e "s|@AGENT_DIR@|$SRC|g" -e "s|@CONFIG_DIR@|$CONFIG_DIR|g" -e "s|@STATE_DIR@|$STATE_DIR|g" \
        "$SRC/systemd/$u" > "$tmp"
    if ! cmp -s "$tmp" "$UNIT_DIR/$u" 2>/dev/null; then
        install -m 644 "$tmp" "$UNIT_DIR/$u"
        say "installed $UNIT_DIR/$u"
    fi
    rm -f "$tmp"
done
if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
    systemctl --user daemon-reload
fi

# --- What is left -----------------------------------------------------------------------------
grep -q '<' "$CONFIG_DIR/config.env" && say "$CONFIG_DIR/config.env still has <placeholders>: fill them in"
cat <<EOF
==> Next (docs/INSTALL.md):
    - Telegram secrets in $MCP_DIR/.env and the login, in your own terminal
    - $CONFIG_DIR/config.env: ids, names, HARNESS and MODEL; log in to that agent CLI
    - systemctl --user enable --now asr-server telegram-mcp telegram-agent telegram-mcp-update.timer
    - loginctl enable-linger \$USER   (keep running without a login session)
EOF
