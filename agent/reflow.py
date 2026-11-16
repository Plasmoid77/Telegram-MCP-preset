"""PostToolUse hook: make large Telegram tool results readable by the bot.

Claude Code saves an MCP result that is too big into a file under this bot's project dir
and hands the model a notice with the path (before this hook runs). telegram-mcp returns
one-line JSON, and Read pages by lines, so the bot could only see the first ~40k chars.
Here we rewrite that file as one record per line next to it and point the bot there.
"""
import json, os, re, sys

# This bot's Claude Code project dir (same rule as guard.py): only files saved there are touched.
ROOT = os.path.realpath(os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects",
    re.sub(r"[^A-Za-z0-9]", "-", os.path.dirname(os.path.realpath(__file__)))))


def as_lines(data):
    """Header line with scalar fields, then one compact JSON object per list item."""
    lists = {k: v for k, v in data.items() if isinstance(v, list) and v and all(isinstance(x, dict) for x in v)}
    if not lists:
        return None, 0
    out = ["# " + json.dumps({k: v for k, v in data.items() if k not in lists}, ensure_ascii=False)]
    n = 0
    for k, items in lists.items():
        out.append(f"# {k}: {len(items)} records, one per line")
        out.extend(json.dumps(x, ensure_ascii=False) for x in items)
        n += len(items)
    return "\n".join(out) + "\n", n


def unwrap(text):
    data = json.loads(text)
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        data = json.loads(data["result"])
    return data if isinstance(data, dict) else None


ev = json.load(sys.stdin)
resp = ev.get("tool_response")
if not isinstance(resp, str):
    sys.exit(0)
m = re.search(r"saved to (/\S+?\.txt)", resp)
if not m:
    # Under the token cap (e.g. MAX_MCP_OUTPUT_TOKENS raised) Claude Code may still save a big
    # result to a file *after* this hook. Reflow it inline so that file is one record per line.
    if len(resp) < 20000:
        sys.exit(0)
    try:
        data = unwrap(resp)
        text, n = as_lines(data) if data else (None, 0)
    except ValueError:
        sys.exit(0)
    if not text:
        sys.exit(0)
    note = (f"# {n} записей, по одной на строку. Если этот результат сохранён в файл, читай его Read "
            f"частями по 50 строк.\n")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "updatedMCPToolOutput": note + text}}, ensure_ascii=False))
    sys.exit(0)
src = os.path.realpath(m.group(1))
if not src.startswith(ROOT + os.sep):
    sys.exit(0)
try:
    with open(src, encoding="utf-8") as f:
        data = unwrap(f.read())
    text, n = as_lines(data) if data else (None, 0)
except (OSError, ValueError):
    sys.exit(0)
if not text:
    sys.exit(0)
dst = src[:-4] + ".lines.txt"
with open(dst, "w", encoding="utf-8") as f:
    f.write(text)
total = text.count("\n")
note = (f"Результат большой ({n} записей, {len(text)} символов), поэтому сохранён в файл {dst} "
        f"по одной записи на строку ({total} строк): первая строка — общие поля, далее «# <ключ>: N records» "
        f"и записи. Прочитай файл целиком инструментом Read частями: offset/limit по 50 строк "
        f"(offset=1, 51, 101, …), пока не дойдёшь до строки {total}.")
print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedMCPToolOutput": note}}, ensure_ascii=False))
