"""Run an isolated headless agent against a single scoped MCP URL."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

HARNESS_NAMES = {"claude", "codex", "opencode", "command"}
CODEX_DISABLED = ("shell_tool", "unified_exec", "view_image", "apps", "multi_agent", "hooks", "goals", "code_mode")


def executable(cfg, harness):
    path = cfg.get(harness.upper() + "_BIN")
    if path:
        return os.path.expanduser(path)
    home = Path.home()
    locations = {
        "codex": (home / ".local/bin/codex", home / ".npm-global/bin/codex"),
        "opencode": (home / ".opencode/bin/opencode", home / ".local/bin/opencode"),
    }
    return next((str(path) for path in locations[harness] if os.access(path, os.X_OK)),
                shutil.which(harness) or harness)


def harness_name(cfg):
    name = cfg.get("HARNESS", "claude")
    if name not in HARNESS_NAMES:
        raise ValueError("HARNESS must be claude, codex, opencode or command")
    return name


def steps_and_timeout(cfg, tier):
    steps = int((cfg.get("OWNER_MAX_TURNS") if tier == "owner" else None) or cfg["MAX_TURNS"])
    timeout = int((cfg.get("OWNER_RUN_TIMEOUT") if tier == "owner" else None) or cfg["RUN_TIMEOUT"])
    if steps <= 0 or timeout <= 0:
        raise ValueError("Agent steps and timeout must be positive")
    return steps, timeout


def custom_command(cfg):
    command = shlex.split(cfg.get("HARNESS_COMMAND", ""))
    if not command:
        raise ValueError("HARNESS_COMMAND must name a trusted runner")
    return command


def opencode_timeouts(cfg):
    return {
        "headerTimeout": int(cfg.get("OPENCODE_HEADER_TIMEOUT_MS", "20000")),
        "chunkTimeout": int(cfg.get("OPENCODE_CHUNK_TIMEOUT_MS", "45000")),
    }


def preflight(cfg):
    name = harness_name(cfg)
    if name != "claude":
        for field in ("OWNER_ID", "SELF_ID"):
            if not cfg.get(field, "").isascii() or not cfg[field].isdigit() or int(cfg[field]) <= 0:
                raise ValueError(f"Set {field} to a positive Telegram user ID")
        if not cfg.get("MODEL"):
            raise ValueError("Set MODEL for the configured harness")
        if name == "opencode" and min(opencode_timeouts(cfg).values()) <= 0:
            raise ValueError("OpenCode provider timeouts must be positive")
        steps_and_timeout(cfg, "guest")
        steps_and_timeout(cfg, "owner")
        binary = custom_command(cfg)[0] if name == "command" else executable(cfg, name)
        if not shutil.which(binary):
            raise ValueError(f"Executable for HARNESS={name} was not found")


def opencode_config(url, names, steps, cfg):
    tool_names = [f"telegram_{name}" for name in names]
    permissions = [{"action": "*", "resource": "*", "effect": "deny"}]
    permissions += [{"action": name, "resource": "*", "effect": "allow"} for name in tool_names]
    policies = [{"action": action, "resource": "*", "effect": "deny"}
                for action in ("tool.use", "mcp.use", "plugin.use", "skill.use", "websearch.use")]
    policies.append({"action": "mcp.use", "resource": "telegram", "effect": "allow"})
    policies += [{"action": "tool.use", "resource": f"{name}:*", "effect": "allow"}
                 for name in tool_names]
    return {
        "providers": {cfg["MODEL"].split("/", 1)[0]: {"settings": opencode_timeouts(cfg)}},
        "permissions": permissions,
        "policies": policies,
        "mcp": {"servers": {"telegram": {
            "type": "remote", "url": url, "oauth": False, "codemode": False,
        }}},
        "agents": {"telegram": {"description": "Telegram responder", "mode": "primary",
                                "steps": steps, "permissions": permissions}},
    }


def launch_args(cfg, harness, directory, url, names, steps, env):
    model = cfg["MODEL"]
    if harness == "codex":
        cmd = [executable(cfg, harness), "exec", "--ignore-user-config", "--ignore-rules",
               "--ephemeral", "--json", "--sandbox", "read-only", "--skip-git-repo-check",
               "--cd", str(directory), "--model", model, "-c", 'approval_policy="never"',
               "-c", 'web_search="disabled"',
               "-c", "mcp_servers.telegram.required=true",
               "-c", 'mcp_servers.telegram.default_tools_approval_mode="approve"',
               "-c", f"mcp_servers.telegram.url={json.dumps(url)}",
               "-c", f"mcp_servers.telegram.enabled_tools={json.dumps(names)}"]
        for feature in CODEX_DISABLED:
            cmd.extend(("--disable", feature))
        return cmd + ["-"]
    if harness == "opencode":
        config = directory / "opencode.json"
        config.write_text(json.dumps(opencode_config(url, names, steps, cfg)), encoding="utf-8")
        # Keep the provider database (saved OpenCode login), but ignore personal config and plugins.
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        env.pop("OPENCODE_CONFIG_DIR", None)
        env.update(OPENCODE_CONFIG=str(config), OPENCODE_CONFIG_PROJECT_DISABLE="1",
                   OPENCODE_DISABLE_CLAUDE_CODE="1", XDG_CONFIG_HOME=str(directory / "config"))
        return [executable(cfg, harness), "run", "--standalone", "--archived",
                "--agent", "telegram", "--model", model, "--format", "json"]
    env.update(TG_MCP_URL=url, TG_MODEL=model, TG_MAX_TURNS=str(steps))
    return custom_command(cfg)


def machine_success(harness, code, lines):
    if code != 0:
        return False
    if harness == "command":
        return True
    kinds = set()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and isinstance(event.get("type"), str):
            kinds.add(event["type"])
    if harness == "codex":
        return "turn.completed" in kinds and not kinds & {"turn.failed", "error"}
    return "step_finish" in kinds and "error" not in kinds


def run(cfg, harness, tier, prompt, url, names):
    steps, timeout = steps_and_timeout(cfg, tier)
    with tempfile.TemporaryDirectory(prefix="telegram-agent-") as tmp:
        directory = Path(tmp)
        env = dict(os.environ, PWD=tmp)
        bypass = ",".join(filter(None, (env.get("no_proxy") or env.get("NO_PROXY"), "127.0.0.1", "localhost")))
        env.update(NO_PROXY=bypass, no_proxy=bypass)
        cmd = launch_args(cfg, harness, directory, url, sorted(names), steps, env)
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace") as output:
            with subprocess.Popen(cmd, cwd=directory, env=env, stdin=subprocess.PIPE,
                                  stdout=output, stderr=subprocess.DEVNULL,
                                  text=True, encoding="utf-8", errors="replace",
                                  start_new_session=True) as process:
                try:
                    process.communicate(prompt, timeout=timeout)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                    raise
            output.seek(0)
            return process.returncode, not machine_success(harness, process.returncode, output)
