"""Relay rules: forward new messages of a source chat to a target chat or forum topic.

Two kinds of rules, merged by source:
- RELAYS in config.env (the admin's): "SOURCE>TARGET[:TOPIC][~REGEX]", forwarded as they are;
- relays.json in the state directory (the owner's, managed from their DM with relay_set /
  relay_remove): besides the target, two plain-language instructions — which messages to forward
  ("select") and what to write under each forwarded one ("comment"). With either of them a
  model run decides on every new message before the dispatcher forwards it.
A config rule wins over a file rule for the same source.
"""
import collections
import functools
import json
import os
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

MAX_INSTRUCTION = 2000  # chars of select / comment
MAX_RULES = 20
MAX_LOG = 50            # decisions kept for relay_list
FIELDS = ("source", "source_name", "target", "target_name", "topic", "select", "comment", "match")


@functools.lru_cache(maxsize=8)
def parse_config(text):
    """RELAYS: space-separated rules "SOURCE>TARGET[:TOPIC][~REGEX]" -> ({source: rule}, [bad rules])."""
    good, bad = [], []
    for raw in text.split():
        m = re.fullmatch(r"(-?\d{1,20})>(-?\d{1,20})(?::([1-9]\d{0,9}))?(?:~(.+))?", raw)
        try:
            if not m or int(m[1]) == int(m[2]):
                raise ValueError(raw)
            rule = {"source": int(m[1]), "target": int(m[2]), "topic": int(m[3]) if m[3] else None,
                    "match": m[4] or "", "select": "", "comment": "", "origin": "config"}
            pattern(rule)
            good.append((raw, rule))
        except (ValueError, OverflowError, re.error):
            bad.append(raw)
    seen = collections.Counter(rule["source"] for _, rule in good)
    bad += [raw for raw, rule in good if seen[rule["source"]] > 1]  # one rule per source
    return {r["source"]: r for _, r in good if seen[r["source"]] == 1}, sorted(bad, key=text.split().index)


@functools.lru_cache(maxsize=64)
def _compile(text):
    return re.compile(text, re.IGNORECASE) if text else None


def pattern(rule):
    return _compile(rule.get("match") or "")


def clean(rule):
    """A rule from relays.json, checked; raises ValueError."""
    if not isinstance(rule, dict):
        raise ValueError("not an object")
    out = {k: rule.get(k) for k in FIELDS}
    for key in ("source", "target"):
        if not isinstance(out[key], int) or isinstance(out[key], bool) or out[key] == 0:
            raise ValueError(key)
    if out["source"] == out["target"]:
        raise ValueError("source = target")
    if out["topic"] is not None and (not isinstance(out["topic"], int) or isinstance(out["topic"], bool)
                                     or out["topic"] <= 0):
        raise ValueError("topic")
    for key in ("source_name", "target_name", "select", "comment", "match"):
        value = out[key] or ""
        if not isinstance(value, str) or len(value) > MAX_INSTRUCTION:
            raise ValueError(key)
        out[key] = value.strip()
    pattern(out)  # raises re.error (a ValueError) on a bad expression
    out["origin"] = "owner"
    return out


def load_file(path):
    """(rules, error): the owner's rules; a broken file gives no rules and says why."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError) as e:
        return [], f"relays.json unreadable: {e}"
    rules, errors = [], []
    for item in data.get("rules", []) if isinstance(data, dict) else []:
        try:
            rules.append(clean(item))
        except (ValueError, TypeError, OverflowError) as e:
            errors.append(str(e))
    return rules, "; ".join(f"bad rule: {e}" for e in errors)


def save_file(path, rules):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"rules": [{k: r.get(k) for k in FIELDS} for r in rules]}
    with NamedTemporaryFile("w", dir=path.parent, prefix=".relays.", delete=False, encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.chmod(f.name, 0o600)
    Path(f.name).replace(path)


RESERVED = {777000, 42777}  # Telegram service chats (login codes): never a source


def merged(cfg, path, previous=None):
    """({source: rule}, notes): config rules over file rules, and what is wrong with either.

    previous: the owner rules last read well; they are kept while relays.json cannot be read, so a
    passing read error does not drop the rules and their queues. The owner's DM, this account and
    Telegram's service chats are never sources: the owner must stay able to talk to the bot.
    """
    rules, bad = parse_config(cfg.get("RELAYS", ""))
    notes = [f"bad RELAYS rule {b!r}" for b in bad]
    owned, error = load_file(path)
    if error:
        notes.append(error)
        if error.startswith("relays.json unreadable") and previous is not None:
            owned = [r for r in previous.values() if r.get("origin") == "owner"]
    reserved = RESERVED | {int(x) for x in (cfg.get("OWNER_ID"), cfg.get("SELF_ID")) if str(x or "").lstrip("-").isdecimal()}
    for source in sorted(set(rules) & reserved):
        notes.append(f"RELAYS rule for {source} ignored: the owner's DM, this account and service chats are never sources")
        del rules[source]
    out = dict(rules)
    for rule in owned:
        if rule["source"] in reserved:
            notes.append(f"relays.json rule for {rule['source']} ignored: not allowed as a source")
            continue
        if rule["source"] in out:
            notes.append(f"relays.json rule for {rule['source']} ignored: RELAYS in config.env has one")
        else:
            out[rule["source"]] = rule
    return out, notes


def ident(rule):
    """Where a rule forwards and what it lets through mechanically: changing it starts it afresh.
    The instructions (select, comment) can change without that."""
    return f"{rule['target']}:{rule.get('topic') or ''}~{rule.get('match') or ''}"


def revision(rule):
    """The instructions a decision was made under: a decision made under others is made again."""
    return json.dumps([rule.get("select") or "", rule.get("comment") or ""], ensure_ascii=False)


def needs_model(rule):
    return bool(rule.get("select") or rule.get("comment"))


def matches(message, rule):
    p = pattern(rule)
    if p is None:
        return True
    urls = [x["url"] for x in message.get("links") or [] if isinstance(x, dict) and isinstance(x.get("url"), str)]
    text = message.get("text") if isinstance(message.get("text"), str) else ""
    return any(p.search(s) for s in [text] + urls)


def log_decision(path, entry):
    """Append one decision to the relay log (the last MAX_LOG are kept)."""
    path = Path(path)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
        items = items if isinstance(items, list) else []
    except (OSError, ValueError):
        items = []
    items = (items + [dict(entry, time=time.strftime("%Y-%m-%d %H:%M"))])[-MAX_LOG:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_log(path, limit=20):
    try:
        items = json.loads(Path(path).read_text(encoding="utf-8"))
        return items[-limit:] if isinstance(items, list) else []
    except (OSError, ValueError):
        return []
