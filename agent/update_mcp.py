#!/usr/bin/env python3
"""telegram-mcp updates: a check that only reports, and a manual upgrade with rollback.

  update_mcp.py check          new upstream release? -> journal + one Telegram message (NOTIFY_CHAT)
  update_mcp.py apply [TAG]    upgrade to TAG (default: newest release), verify, roll back on failure
  update_mcp.py status         installed and newest release

Nothing is ever installed automatically: the server holds the whole Telegram account, so new
upstream code runs only after the owner has read what changed. `check` runs from
telegram-mcp-update.timer; `apply` is run by hand.

`apply`: waits until no bot run is in progress, stops telegram-agent, checks out the tag,
`uv sync`, restarts telegram-mcp, calls the tools the dispatcher depends on, starts
telegram-agent. Any failure: back to the previous version.

Part of Telegram-MCP-preset: https://github.com/Plasmoid77/Telegram-MCP-preset
"""
import fcntl, json, re, shutil, subprocess, sys, time, urllib.parse
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))  # systemd runs python -I
import config  # noqa: E402
from mcp import Mcp  # noqa: E402

MCP_DIR = Path.home() / ".local/share/telegram-mcp"
STATE = config.STATE_DIR / "mcp_update.json"
LOCK = config.STATE_DIR / "mcp_update.lock"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
MAX_CHANGES = 20       # lines of the change list in the Telegram message
IDLE_WAIT = 900        # s to wait for a running bot reply before giving up
READY_WAIT = 120       # s for the restarted server to answer
AGENT_WAIT = 15        # s telegram-agent must stay up after the upgrade


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


def sh(*cmd, cwd=None, check=True):
    """Run a command, return stdout (stripped). check=True raises with its stderr."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()[:500]}")
    return r.stdout.strip()


def git(*args, check=True):
    return sh("git", "-C", str(MCP_DIR), *args, check=check)


def version(tag):
    m = TAG_RE.match(tag or "")
    return tuple(map(int, m.groups())) if m else None


def newest(tags):
    """Newest release tag (vX.Y.Z only) or None."""
    rel = [t for t in tags if version(t)]
    return max(rel, key=version) if rel else None


def installed():
    """(tag or None, commit) of the live checkout."""
    commit = git("rev-parse", "HEAD")
    tags = git("tag", "--points-at", "HEAD").split()
    return newest(tags), commit


def fetch():
    git("fetch", "--quiet", "--tags", "origin")
    return git("tag", "--list", "v*").split()


def changes(old, new):
    """One line per merged PR (or direct commit) between two refs, newest first."""
    out = []
    text = git("log", "--first-parent", "--format=%s%x1f%b%x1e", f"{old}..{new}")
    for rec in text.split("\x1e"):
        if not rec.strip():
            continue
        subj, _, body = rec.strip().partition("\x1f")
        m = re.match(r"Merge pull request (#\d+)", subj)
        title = next((l.strip() for l in body.splitlines() if l.strip()), "") if m else ""
        out.append(f"{m.group(1)} {title}".strip() if m else subj)
    return out


def repo_url():
    url = git("remote", "get-url", "origin", check=False)
    url = re.sub(r"\.git$", "", url)
    return url if url.startswith("https://github.com/") else None


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(st):
    STATE.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def notice(cur, latest, lines):
    """Text of the Telegram message about a new release."""
    shown = lines[:MAX_CHANGES]
    more = len(lines) - len(shown)
    parts = [f"telegram-mcp: вышла {latest} (установлена {cur or 'не тег'})."]
    if shown:
        parts += ["", "Изменения:"] + [f"• {l}" for l in shown]
        if more > 0:
            parts.append(f"… и ещё {more}")
    url = repo_url()
    if url and cur:
        parts += ["", f"{url}/compare/{cur}...{latest}"]
    parts += ["", "Само не обновляется. Обновить (с проверкой и откатом):",
              f"python3 {BASE / 'update_mcp.py'} apply {latest}"]
    return "\n".join(parts)


# --- check ------------------------------------------------------------------------------------
def check(mcp=None, cfg=None):
    """Log the state; message the owner once per new release. Returns the exit code."""
    tags = fetch()
    latest = newest(tags)
    cur, commit = installed()
    if not latest:
        log("no release tags upstream")
        return 1
    if cur and version(cur) >= version(latest):
        log(f"telegram-mcp {cur} is the newest release")
        return 0
    base = cur or commit[:12]
    lines = changes(cur or commit, latest)
    log(f"telegram-mcp {latest} is out (installed {base}), {len(lines)} changes:")
    for l in lines:
        log(f"  {l}")
    st = load_state()
    if st.get("notified") == latest:
        log("owner already notified about this release")
        return 0
    cfg = cfg or config.load()
    chat = cfg.get("NOTIFY_CHAT") or cfg.get("OWNER_ID")
    if not chat or "<" in chat:
        log("NOTIFY_CHAT is not set in config.env: not messaging anyone")
        return 0
    try:
        reply = (mcp or Mcp(cfg["MCP_URL"])).call(
            "send_message", {"chat_id": chat if chat == "me" else int(chat),
                             "message": notice(cur, latest, lines)}, raw=True)
        if not reply.startswith("Message sent"):
            raise RuntimeError(reply[:300])
    except Exception as e:  # server down etc.: try again on the next check
        log(f"could not message {chat}: {e}")
        return 1
    st["notified"] = latest
    save_state(st)
    log(f"owner notified in chat {chat}")
    return 0


# --- apply ------------------------------------------------------------------------------------
def systemctl(*args, check=True):
    return sh("systemctl", "--user", *args, check=check)


def pause_agent(timeout=IDLE_WAIT):
    """Ask the dispatcher to start no new runs; True once it confirms that none is running.

    The dispatcher checks the pause file before every run and, while paused, writes the ack
    file at the end of each loop step. Runs are synchronous, so an ack means no run is in
    progress and none will start: stopping telegram-agent then kills nothing.
    """
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    global OWN_PAUSE
    OWN_PAUSE = OWN_PAUSE or not config.PAUSE.exists()  # someone else's pause is theirs to lift
    config.PAUSE_ACK.unlink(missing_ok=True)
    config.PAUSE.touch()
    if systemctl("is-active", "telegram-agent", check=False) != "active":
        return True
    end = time.monotonic() + timeout
    said = False
    while not config.PAUSE_ACK.exists():
        if time.monotonic() > end:
            return False
        if not said:
            log("waiting for the bot to finish its current reply")
            said = True
        time.sleep(1)
    return True


OWN_PAUSE = False


def resume_agent():
    """Lift the pause only if this run set it (an operator's own pause stays)."""
    global OWN_PAUSE
    if OWN_PAUSE:
        config.PAUSE.unlink(missing_ok=True)
        config.PAUSE_ACK.unlink(missing_ok=True)
    OWN_PAUSE = False


def smoke(cfg, mcp=None, wait=READY_WAIT):
    """Call what the dispatcher depends on; raise on anything unexpected."""
    mcp = mcp or Mcp(cfg["MCP_URL"])
    end = time.monotonic() + wait
    while True:
        try:
            me = mcp.call("get_me", {})
            break
        except Exception as e:
            if time.monotonic() > end:
                raise RuntimeError(f"server does not answer: {e}")
            mcp.sid = None
            time.sleep(3)
    self_id = cfg.get("SELF_ID", "")
    if self_id.isdigit() and int(me.get("id", 0)) != int(self_id):
        raise RuntimeError(f"get_me: logged in as {me.get('id')}, SELF_ID is {self_id}")
    ev = mcp.call("wait_for_events", {"after_seq": -1})
    if "boot" not in ev or "last_seq" not in ev:
        raise RuntimeError(f"wait_for_events: no boot/last_seq in {sorted(ev)}")
    chats = json.loads(mcp.call("list_chats", {"limit": 3}, raw=True))
    if not isinstance(chats.get("results"), list):
        raise RuntimeError("list_chats: no results list")
    owner = cfg.get("OWNER_ID", "")
    if owner.isdigit():
        r = mcp.call("get_messages_after", {"chat_id": int(owner), "limit": 2})
        msgs = r.get("messages")
        if not isinstance(msgs, list) or any("id" not in m or "date" not in m for m in msgs):
            raise RuntimeError("get_messages_after: unexpected message format")
    mcp.call("list_folders", {}, raw=True)
    return me


def switch(ref):
    git("-c", "advice.detachedHead=false", "checkout", "--quiet", ref)
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    sh(uv, "sync", "--quiet", cwd=str(MCP_DIR))


def restart_and_verify(cfg, ref, agent):
    if config.PAUSE.exists():
        config.PAUSE.touch()  # keep the pause fresh (the dispatcher ignores one older than 30 min)
    switch(ref)
    systemctl("restart", "telegram-mcp")
    smoke(cfg)
    if not agent:
        return
    systemctl("start", "telegram-agent")
    time.sleep(AGENT_WAIT)
    if systemctl("is-active", "telegram-agent", check=False) != "active":
        raise RuntimeError("telegram-agent did not stay up (journalctl --user -u telegram-agent)")


def apply(target=None, assume_yes=False):
    """Upgrade under an exclusive lock: two applies at once would undo each other."""
    LOCK.parent.mkdir(mode=0o700, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another update_mcp.py apply is running")
            return 1
        try:
            return _apply(target, assume_yes)
        finally:
            resume_agent()


def _apply(target, assume_yes):
    cfg = config.load()
    if urllib.parse.urlsplit(cfg["MCP_URL"]).hostname not in ("127.0.0.1", "localhost", "::1"):
        log(f"MCP_URL {cfg['MCP_URL']} is not this machine's telegram-mcp: the upgrade could not be verified")
        return 1
    if git("status", "--porcelain", "--untracked-files=no"):
        log(f"{MCP_DIR} has local changes: commit or discard them first (git -C {MCP_DIR} status)")
        return 1
    tags = fetch()
    target = target or newest(tags)
    if target not in tags:
        log(f"no tag {target} upstream")
        return 1
    cur, commit = installed()
    if cur == target:
        log(f"telegram-mcp is already at {target}")
        return 0
    if cur and version(target) < version(cur):
        log(f"note: {target} is older than the installed {cur} (downgrade)")
    lines = changes(cur or commit, target) if not (cur and version(target) < version(cur)) else []
    print(f"telegram-mcp {cur or commit[:12]} -> {target}")
    for l in lines:
        print(f"  {l}")
    if not assume_yes:
        if not sys.stdin.isatty():
            log("not a terminal: pass --yes to confirm")
            return 1
        if input("Обновить? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
            log("cancelled")
            return 1
    try:
        smoke(cfg, wait=10)  # the current version must work now, or a failure proves nothing
    except Exception as e:
        log(f"telegram-mcp does not work even before the upgrade, not touching it: {e}")
        return 1
    if not pause_agent():
        log("the bot did not confirm the pause in 15 min (busy?): try later")
        return 1
    agent = systemctl("is-active", "telegram-agent", check=False) == "active"
    log(f"upgrading telegram-mcp {cur or commit[:12]} -> {target}")
    systemctl("stop", "telegram-agent")
    try:
        restart_and_verify(cfg, target, agent)
    except Exception as e:
        log(f"upgrade failed: {e}")
        log(f"rolling back to {cur or commit[:12]}")
        systemctl("stop", "telegram-agent", check=False)  # still paused: no run to kill
        try:
            restart_and_verify(cfg, cur or commit, agent)
        except Exception as e2:
            if agent:
                systemctl("start", "telegram-agent", check=False)
            log(f"ROLLBACK FAILED: {e2}. Check: systemctl --user status telegram-mcp telegram-agent")
            return 2
        log(f"rolled back, telegram-mcp {cur or commit[:12]} works")
        return 1
    log(f"telegram-mcp {target} is running, the bot is up. The tag in install.sh is still the one "
        "the preset was verified with")
    return 0


def status():
    tags = fetch()
    cur, commit = installed()
    latest = newest(tags)
    print(f"installed: {cur or 'untagged ' + commit[:12]}, newest release: {latest}")
    return 0


def main(argv):
    cmd = argv[0] if argv else "status"
    rest = [a for a in argv[1:] if a not in ("-y", "--yes")]
    try:
        if cmd == "check":
            return check()
        if cmd == "apply":
            return apply(rest[0] if rest else None, assume_yes=len(rest) != len(argv[1:]))
        if cmd == "status":
            return status()
    except RuntimeError as e:
        log(f"error: {e}")
        return 1
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
