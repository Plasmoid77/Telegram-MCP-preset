"""Persist one open owner-DM task as Telegram message references."""

import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile


MAX_IDS = 8
MAX_TEXT = 1500
STATUSES = {"pending", "delivery_unknown", "sent"}


def message_ids(ids):
    ids = tuple(sorted({value for value in ids if type(value) is int and value > 0}))
    # Preserve the original instruction as later clarifications accumulate.
    return ids[:1] + ids[1:][-(MAX_IDS - 1):]


class Followups:
    def __init__(self, path, owner, account):
        self.path = path
        self.identity = {"owner": str(owner), "account": str(account)}

    def read(self):
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(record, dict) or record.get("identity") != self.identity:
            return None
        ids = record.get("message_ids")
        status = record.get("status")
        if (not isinstance(ids, list) or tuple(ids) != message_ids(ids) or not ids
                or not isinstance(status, str) or status not in STATUSES):
            raise ValueError("Invalid follow-up state")
        return record

    def write(self, ids, status="pending", *, delivered_id=None, question_id=None):
        record = {"identity": self.identity, "message_ids": list(message_ids(ids)), "status": status}
        if delivered_id is not None:
            record["delivered_id"] = delivered_id
        if question_id is not None:
            record["question_id"] = question_id
        with NamedTemporaryFile(dir=self.path.parent, mode="w", encoding="utf-8",
                                prefix=".owner-followup.", delete=False) as stream:
            staged = Path(stream.name)
            try:
                json.dump(record, stream)
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
                staged.replace(self.path)
                directory = os.open(self.path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                staged.unlink(missing_ok=True)

    def clear(self):
        self.path.unlink(missing_ok=True)


def restore(upstream, chat, account, record):
    anchors = [(mid, False) for mid in record["message_ids"]]
    if type(record.get("question_id")) is int and record["question_id"] > 0:
        anchors.append((record["question_id"], True))
    rows = []
    for mid, outgoing in sorted(anchors):
        response = upstream.call("get_messages_after", {"chat_id": chat, "after_id": mid - 1, "limit": 1},
                                 timeout=2)
        if str(response.get("chat_id")) != str(chat):
            raise ValueError("Unexpected chat in follow-up lookup")
        for message in response.get("messages", []):
            text = message.get("text")
            sender = account if outgoing else chat
            if (message.get("id") == mid and str(message.get("sender_id")) == str(sender)
                    and bool(message.get("out")) == outgoing and not message.get("forwarded")
                    and isinstance(text, str)):
                rows.append({"id": mid, "author": "bot" if outgoing else "owner",
                             "text": text[:MAX_TEXT], "truncated": len(text) > MAX_TEXT})
                break
        else:
            raise ValueError("Follow-up message is missing")
    return rows


def delivery_id(result):
    if result.get("isError"):
        return None
    for item in result.get("content", []):
        text = item.get("text", "")
        if not isinstance(text, str):
            continue
        match = re.fullmatch(r"Message sent successfully\.\s*message_id=(\d+)\s*", text)
        if match:
            return int(match[1])
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("sent") is True and type(data.get("message_id")) is int:
            return data["message_id"]
    return None
