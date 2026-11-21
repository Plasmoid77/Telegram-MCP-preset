#!/usr/bin/env bash
# Move a v1 install (files copied into ~/.local/share/telegram-agent) to the v2 layout.
#
# Usage: clone the new version next to the old one and run this script from that clone:
#   git clone -b <tag> https://github.com/Plasmoid77/Telegram-MCP-preset ~/.local/share/telegram-agent.new
#   ~/.local/share/telegram-agent.new/scripts/migrate-v1.sh
#
# Steps (the bot is offline only between 4 and 8, about a minute):
#  1. checks the copied settings against the running telegram-mcp (dispatcher.py --check)
#  2. prepares asr-server's environment in the new checkout while the bot still runs
#  3. writes the rollback script, stops the weekly update check
#  4. pauses the bot (waits for its current reply), then stops it
#  5. copies settings to ~/.config/telegram-agent, state + memory to ~/.local/state/telegram-agent
#  6. keeps v1 and its units as ~/.local/share/telegram-agent.v1-<time>, moves the new checkout in
#  7. runs install.sh (units now point at the checkout)
#  8. restarts telegram-mcp (its unit changed) and asr-server, starts the bot, checks it stays up
#
# Nothing is deleted. A failure before step 6 puts v1 back by itself; later, run
# ~/.local/share/telegram-agent.v1-<time>/rollback.sh
set -euo pipefail

NEW=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OLD=$HOME/.local/share/telegram-agent
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/telegram-agent
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/telegram-agent
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=$OLD.v1-$STAMP
UNITS=(telegram-mcp.service telegram-agent.service asr-server.service telegram-mcp-update.service telegram-mcp-update.timer)
SYSTEMCTL=${SYSTEMCTL:-systemctl}
phase=checks agent_was_active=0 timer_was_active=0

say() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
active() { [[ $($SYSTEMCTL --user is-active "$1" 2>/dev/null) == active ]]; }
on_error() {
    case $phase in
        checks) ;;
        stopped)  # nothing moved yet: bring v1 back as it was
            echo "migration failed before anything moved: restarting v1" >&2
            rm -f "$OLD/state/pause" "$OLD/state/pause.ack"
            [[ $agent_was_active == 1 ]] && $SYSTEMCTL --user start telegram-agent
            [[ $timer_was_active == 1 ]] && $SYSTEMCTL --user start telegram-mcp-update.timer ;;
        *) echo "migration failed during '$phase': run $BACKUP/rollback.sh to return to v1" >&2 ;;
    esac
}
trap on_error ERR

[[ -f $OLD/config.env && ! -e $OLD/.git ]] || die "no v1 install in $OLD (nothing to migrate)"
[[ $NEW != "$OLD" ]] || die "run this from a new checkout next to $OLD, not from it"
[[ -e $NEW/agent/dispatcher.py && -e $NEW/agent/broker.py ]] || die "$NEW is not a v2 checkout"
for f in config.env backend.env; do
    [[ ! -e $CONFIG_DIR/$f ]] || die "$CONFIG_DIR/$f already exists: move it away first"
done
if pgrep -f "update_mcp.py apply" >/dev/null; then die "update_mcp.py apply is running: wait for it"; fi

say "1/8 checking the settings with the new version"
if [[ ${TG_MIGRATE_CHECK:-1} != 0 ]]; then
    tmp=$(mktemp -d)
    install -m 600 "$OLD/config.env" "$tmp/config.env"
    TG_AGENT_CONFIG_DIR=$tmp TG_AGENT_STATE_DIR=$tmp python3 -I "$NEW/agent/dispatcher.py" --check \
        || { rm -rf "$tmp"; die "fix ~/.local/share/telegram-agent/config.env for v2 (see above) and run again; nothing changed"; }
    rm -rf "$tmp"
fi

say "2/8 preparing asr-server's environment in $NEW (the bot keeps running)"
(cd "$NEW/asr" && uv sync --quiet --frozen)

say "3/8 writing $BACKUP/rollback.sh, stopping the weekly update check"
if active telegram-mcp-update.timer; then timer_was_active=1; fi
if active telegram-agent; then agent_was_active=1; fi
rollback=$(mktemp)
cat > "$rollback" <<EOF
#!/usr/bin/env bash
# Back to v1 as it was before the migration on $STAMP. Brings v2's chat cursors and memory
# along, so nothing is answered twice and nothing remembered is lost.
set -euo pipefail
S=$STATE_DIR
if [[ \$($SYSTEMCTL --user is-active telegram-agent 2>/dev/null) == active ]]; then
    rm -f "\$S/pause.ack"; touch "\$S/pause"
    for _ in \$(seq 900); do [[ -e \$S/pause.ack ]] && break; sleep 1; done
fi
$SYSTEMCTL --user stop telegram-agent telegram-mcp-update.timer || true
rm -f "\$S/pause" "\$S/pause.ack"
if [[ -e "$OLD/agent/broker.py" ]]; then mv "$OLD" "$OLD.v2-\$(date +%Y%m%d-%H%M%S)"; fi
if [[ ! -e "$OLD" ]]; then mv "$BACKUP" "$OLD"; fi
[[ -e \$S/cursors.json ]] && cp "\$S/cursors.json" "$OLD/state/cursors.json"
[[ -e \$S/memory.md ]] && cp "\$S/memory.md" "$OLD/memory.md"
cp -p "$OLD/units/"* "$UNIT_DIR/"
$SYSTEMCTL --user daemon-reload
$SYSTEMCTL --user restart telegram-mcp asr-server
[[ $agent_was_active == 1 ]] && $SYSTEMCTL --user start telegram-agent
[[ $timer_was_active == 1 ]] && $SYSTEMCTL --user start telegram-mcp-update.timer
echo "v1 is back"
EOF
if [[ $timer_was_active == 1 ]]; then $SYSTEMCTL --user stop telegram-mcp-update.timer; fi
phase=stopped

say "4/8 pausing the bot"
if [[ $agent_was_active == 1 ]]; then
    rm -f "$OLD/state/pause.ack"
    touch "$OLD/state/pause"
    for _ in $(seq "${TG_MIGRATE_PAUSE_WAIT:-900}"); do [[ -e $OLD/state/pause.ack ]] && break; sleep 1; done
    [[ -e $OLD/state/pause.ack ]] || false  # -> on_error restarts v1 as it was
    $SYSTEMCTL --user stop telegram-agent
fi
rm -f "$OLD/state/pause" "$OLD/state/pause.ack"

say "5/8 copying settings to $CONFIG_DIR and state to $STATE_DIR"
install -d -m 700 "$CONFIG_DIR" "$STATE_DIR"
install -m 600 "$OLD/config.env" "$CONFIG_DIR/config.env"
[[ -e $OLD/backend.env ]] && install -m 600 "$OLD/backend.env" "$CONFIG_DIR/backend.env"
[[ -e $OLD/prompt.md ]] && install -m 600 "$OLD/prompt.md" "$CONFIG_DIR/prompt.v1.md"
[[ -e $OLD/memory.md ]] && install -m 600 "$OLD/memory.md" "$STATE_DIR/memory.md"
for f in cursors.json mcp_update.json; do
    [[ -e $OLD/state/$f ]] && install -m 600 "$OLD/state/$f" "$STATE_DIR/$f"
done
if [[ -e $OLD/state/owner-followup.json ]]; then
    say "NOTE: v1 had an open owner follow-up (state/owner-followup.json, kept in the backup)."
    say "      v2 does not resume it: check that conversation and repeat the request if needed."
fi
if [[ -e $OLD/mcp.json ]] && ! grep -q '127.0.0.1:8765/mcp' "$OLD/mcp.json"; then
    say "NOTE: your v1 mcp.json pointed elsewhere; set MCP_URL in $CONFIG_DIR/config.env"
fi

say "6/8 keeping v1 as $BACKUP, moving $NEW into $OLD"
phase=moving
install -m 755 "$rollback" "$OLD/rollback.sh"
rm -f "$rollback"
mkdir -p "$OLD/units"
for u in "${UNITS[@]}"; do [[ -e $UNIT_DIR/$u ]] && cp -p "$UNIT_DIR/$u" "$OLD/units/"; done
mv "$OLD" "$BACKUP"
mv "$NEW" "$OLD"

say "7/8 installing v2 from $OLD"
phase=installing
"$OLD/install.sh"

say "8/8 restarting the services"
phase=starting
$SYSTEMCTL --user restart telegram-mcp asr-server
if [[ $agent_was_active == 1 ]]; then
    $SYSTEMCTL --user start telegram-agent
    sleep "${TG_MIGRATE_SETTLE:-20}"
    active telegram-agent || false  # -> on_error points at rollback.sh
    if [[ ${TG_MIGRATE_CHECK:-1} != 0 ]]; then python3 -I "$OLD/agent/dispatcher.py" --check; fi
fi
if [[ $timer_was_active == 1 ]]; then $SYSTEMCTL --user start telegram-mcp-update.timer; fi
phase=finished
cat <<EOF
==> Migrated. Watch: journalctl --user -u telegram-agent -f
    Then check: an owner DM, a guest DM, a group mention, a voice message, «запомни …».
    Your v1 prompt is in $CONFIG_DIR/prompt.v1.md for reference; v2 reads agent/prompts/
    (put your own persona.md in $CONFIG_DIR to override the persona).
    The old feed file ~/.local/state/telegram-mcp/incoming_feed.jsonl is no longer written; delete it when you like.
    Rollback: $BACKUP/rollback.sh
EOF
