"""Run one headless agent turn against a single broker URL.

Adapters: claude (Claude Code), codex (Codex CLI), opencode (OpenCode), command (your own runner).
Each one is started with no built-in tools, no personal configuration, and only the broker as
its MCP server. Instructions go to a file or stdin, never to the command line.
"""
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

NAMES = ("claude", "codex", "opencode", "command")
CODEX_DISABLED = ("shell_tool", "unified_exec", "view_image", "apps", "multi_agent", "hooks", "goals", "code_mode")
OPENCODE_RETRY_HEADER_TIMEOUT_MS = 300_000


class Result(NamedTuple):
    ok: bool
    rc: int
    transient: bool   # failed in a way worth one more attempt (provider timeout, no answer)
    stderr: str = ""  # the end of the CLI's stderr, for the journal when a run fails


TRANSIENT = ("timeout", "timed out", "headers", "overloaded", "rate limit", "econnreset", "503", "529")


def name(cfg):
    harness = cfg.get("HARNESS", "claude")
    if harness not in NAMES:
        raise ValueError("HARNESS must be one of " + ", ".join(NAMES))
    return harness


def executable(cfg, harness):
    path = cfg.get(harness.upper() + "_BIN")
    if path:
        return os.path.expanduser(path)
    home = Path.home()
    for p in {"claude": (home / ".local/bin/claude",),
              "codex": (home / ".local/bin/codex", home / ".npm-global/bin/codex"),
              "opencode": (home / ".opencode/bin/opencode", home / ".local/bin/opencode")}[harness]:
        if os.access(p, os.X_OK):
            return str(p)
    return shutil.which(harness) or harness


def limits(cfg, owner):
    steps = int(cfg["OWNER_MAX_TURNS"] if owner else cfg["MAX_TURNS"])
    timeout = int(cfg["OWNER_RUN_TIMEOUT"] if owner else cfg["RUN_TIMEOUT"])
    if steps <= 0 or timeout <= 0:
        raise ValueError("MAX_TURNS and RUN_TIMEOUT must be positive")
    return steps, timeout


def preflight(cfg):
    """Fail early on a config the dispatcher cannot run with."""
    harness = name(cfg)
    for key in ("OWNER_ID", "SELF_ID"):
        if not cfg.get(key, "").isdecimal():
            raise ValueError(f"Set {key} to the numeric Telegram user id")
    if harness != "claude" and not cfg.get("MODEL"):
        raise ValueError("Set MODEL for HARNESS=" + harness)
    limits(cfg, False)
    limits(cfg, True)
    binary = shlex.split(cfg.get("HARNESS_COMMAND", ""))[:1] if harness == "command" else [executable(cfg, harness)]
    if not binary or not shutil.which(binary[0]):
        raise ValueError(f"Executable for HARNESS={harness} was not found")


def opencode_config(cfg, url, tools, steps, header_ms):
    names = [f"telegram_{t}" for t in tools]
    permissions = [{"action": "*", "resource": "*", "effect": "deny"}]
    permissions += [{"action": n, "resource": "*", "effect": "allow"} for n in names]
    policies = [{"action": a, "resource": "*", "effect": "deny"}
                for a in ("tool.use", "mcp.use", "plugin.use", "skill.use", "websearch.use")]
    policies.append({"action": "mcp.use", "resource": "telegram", "effect": "allow"})
    policies += [{"action": "tool.use", "resource": f"{n}:*", "effect": "allow"} for n in names]
    settings = {"headerTimeout": header_ms}
    if cfg.get("OPENCODE_CHUNK_TIMEOUT_MS"):
        settings["chunkTimeout"] = int(cfg["OPENCODE_CHUNK_TIMEOUT_MS"])
    return {
        "providers": {cfg["MODEL"].split("/", 1)[0]: {"settings": settings}},
        "permissions": permissions, "policies": policies,
        "mcp": {"servers": {"telegram": {"type": "remote", "url": url, "oauth": False, "codemode": False}}},
        "agents": {"telegram": {"description": "Telegram responder", "mode": "primary",
                                "steps": steps, "permissions": permissions}},
    }


def command(cfg, harness, tmp, url, tools, steps, instructions, task, env, retry):
    """(argv, stdin text) for one run; writes the adapter's files into tmp."""
    model = cfg.get("MODEL", "")
    if harness == "claude":
        (tmp / "system.md").write_text(instructions, encoding="utf-8")
        (tmp / "mcp.json").write_text(json.dumps({"mcpServers": {"telegram": {"type": "http", "url": url}}}))
        env.update(CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
        argv = [executable(cfg, harness), "-p", "--system-prompt-file", str(tmp / "system.md"),
                "--strict-mcp-config", "--mcp-config", str(tmp / "mcp.json"),
                "--settings", json.dumps({"autoMemoryEnabled": False}), "--setting-sources", "project",
                "--tools", "", "--allowedTools", "mcp__telegram",
                "--max-turns", str(steps), "--output-format", "json",
                "--no-session-persistence", "--disable-slash-commands"]
        return argv + (["--model", model] if model else []), task
    prompt = instructions + "\n\n# Задача\n" + task
    if harness == "codex":
        argv = [executable(cfg, harness), "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(tmp),
                "--model", model, "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                "-c", "mcp_servers.telegram.required=true",
                "-c", 'mcp_servers.telegram.default_tools_approval_mode="approve"',
                "-c", f"mcp_servers.telegram.url={json.dumps(url)}",
                "-c", f"mcp_servers.telegram.enabled_tools={json.dumps(tools)}"]
        for feature in CODEX_DISABLED:
            argv += ["--disable", feature]
        return argv + ["-"], prompt
    if harness == "opencode":
        header = OPENCODE_RETRY_HEADER_TIMEOUT_MS if retry else int(cfg.get("OPENCODE_HEADER_TIMEOUT_MS") or 20000)
        conf = tmp / "opencode.json"
        conf.write_text(json.dumps(opencode_config(cfg, url, tools, steps, header)), encoding="utf-8")
        # Keep the provider login (data dir) but ignore personal config and plugins.
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        env.pop("OPENCODE_CONFIG_DIR", None)
        env.update(OPENCODE_CONFIG=str(conf), OPENCODE_CONFIG_PROJECT_DISABLE="1",
                   OPENCODE_DISABLE_CLAUDE_CODE="1", XDG_CONFIG_HOME=str(tmp / "config"))
        return [executable(cfg, harness), "run", "--standalone", "--archived", "--agent", "telegram",
                "--model", model, "--format", "json"], prompt
    env.update(TG_MCP_URL=url, TG_MODEL=model, TG_MAX_TURNS=str(steps))
    return shlex.split(cfg["HARNESS_COMMAND"]), prompt


def outcome(harness, rc, output):
    """Result from the exit code and the CLI's machine-readable output."""
    if harness == "command":
        return Result(rc == 0, rc, False)
    if harness == "claude":
        try:
            r = json.loads(output)
            return Result(rc == 0 and not r.get("is_error"), rc, r.get("subtype") == "error_during_execution")
        except ValueError:
            return Result(False, rc, True)
    kinds, transient = set(), False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            kinds.add(event["type"])
            if event["type"] in ("error", "turn.failed") and any(
                    p in json.dumps(event).lower() for p in TRANSIENT):
                transient = True
    if harness == "codex":
        return Result(rc == 0 and "turn.completed" in kinds and not kinds & {"turn.failed", "error"}, rc, transient)
    return Result(rc == 0 and "step_finish" in kinds and "error" not in kinds, rc, transient)


def run(cfg, owner, instructions, task, url, tools, timeout, retry=False):
    """One agent turn. Raises subprocess.TimeoutExpired after timeout seconds (process group killed)."""
    harness = name(cfg)
    steps, _ = limits(cfg, owner)
    with tempfile.TemporaryDirectory(prefix="telegram-agent-") as d:
        tmp = Path(d)
        env = dict(os.environ, PWD=d)
        bypass = ",".join(filter(None, (env.get("no_proxy") or env.get("NO_PROXY"), "127.0.0.1", "localhost")))
        env.update(NO_PROXY=bypass, no_proxy=bypass)
        argv, stdin = command(cfg, harness, tmp, url, sorted(tools), steps, instructions, task, env, retry)
        with tempfile.TemporaryFile("w+t", encoding="utf-8", errors="replace") as out, \
                tempfile.TemporaryFile("w+t", encoding="utf-8", errors="replace") as err:
            with subprocess.Popen(argv, cwd=d, env=env, stdin=subprocess.PIPE, stdout=out,
                                  stderr=err, text=True, encoding="utf-8",
                                  errors="replace", start_new_session=True) as p:
                try:
                    p.communicate(stdin, timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    p.communicate()
                    raise
            out.seek(0)
            err.seek(0)
            return outcome(harness, p.returncode, out.read())._replace(stderr=err.read()[-2000:])
