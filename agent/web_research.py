"""Read-only web research from the owner's current DM and an opted-in public model ID."""

from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
import json
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlsplit


QUERY = re.compile(r"^/web(?:\s+|$)", re.IGNORECASE)
MAX_QUERY = 400
MAX_RESULTS = 6
HOST = "lite.duckduckgo.com"
DASHES = str.maketrans("‐‑‒–—−", "------")
MODEL_ID = re.compile(r"[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*")
VERSIONED_NAME = re.compile(
    r"\b([a-z][a-z0-9]*?)[ _-]*(\d+(?:\.\d+)+)"
    r"(?:[ _-]+([a-z][a-z0-9]*(?:-[a-z0-9]+)*))?(?!\w|\.\d|-\w)",
    re.IGNORECASE | re.ASCII,
)


def versioned_name_key(match):
    name, version, variant = match.groups()
    name = name.lower()
    return ("gpt" if name == "chatgpt" else name, version, (variant or "").lower())


def authored_text(message):
    if message.get("forwarded") or any(
        entity.get("type") in ("blockquote", "code", "pre")
        for entity in (message.get("formatting") or [])
    ):
        return ""
    return " ".join((message.get("text") or "").split())


def requested_query(message):
    """Return the literal /web query, or None for a normal conversation."""
    text = authored_text(message)
    prefix = QUERY.match(text)
    return " ".join(text[prefix.end():].split()) if prefix else None


def public_model(cfg):
    """Use only the model ID, excluding the provider prefix, when explicitly public."""
    if cfg.get("WEB_SEARCH_PUBLIC_MODEL") != "1":
        return ""
    model = cfg.get("MODEL", "").rpartition("/")[2]
    return model if len(model) <= MAX_QUERY and MODEL_ID.fullmatch(model) else ""


def model_query_key(query):
    return re.sub(r"[ _-]+", "-", query.translate(DASHES).casefold())


def query_from_messages(query, messages):
    """Send exactly the author's words, even when the model changes whitespace or case."""
    if not isinstance(query, str):
        return None
    query = " ".join(query.split())
    if not 0 < len(query) <= MAX_QUERY:
        return None
    query = query.translate(DASHES)
    normalized = [(message, message.translate(DASHES)) for message in messages]
    requested = {versioned_name_key(match) for match in VERSIONED_NAME.finditer(query)}
    boundary = r"(?!\w|\.\d|-\w)" if requested else r"(?!\w)"
    pattern = re.compile(r"(?<!\w)" + re.escape(query) + boundary, re.IGNORECASE)
    for message, normalized_message in normalized:
        match = pattern.search(normalized_message)
        if match:
            return message[match.start():match.end()]
    # Recover the complete shared versioned name, without transmitting added words
    # or accepting a different version or variant.
    if requested:
        for message, normalized_message in normalized:
            for match in VERSIONED_NAME.finditer(normalized_message):
                if len(match[0]) <= MAX_QUERY and versioned_name_key(match) in requested:
                    return message[match.start():match.end()]
    return None


def result_url(href):
    try:
        url = urlsplit(href)
        if url.hostname in ("duckduckgo.com", HOST) and url.path == "/l/":
            href = parse_qs(url.query).get("uddg", [""])[0]
            url = urlsplit(href)
        if (url.scheme in ("https", "http") and url.hostname and url.username is None
                and len(href) <= 2048 and not any(ch.isspace() for ch in href)):
            return href
    except ValueError:
        pass
    return ""


class Results(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.current = None
        self.field = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "a" and "result-link" in classes:
            href = result_url(attrs.get("href") or "")
            self.current = {"url": href, "title": "", "snippet": ""} if href else None
            if self.current is not None and len(self.results) < MAX_RESULTS:
                self.results.append(self.current)
                self.field = "title"
        if tag == "td" and "result-snippet" in classes and self.current is not None:
            self.field = "snippet"

    def handle_endtag(self, tag):
        if (tag, self.field) in (("a", "title"), ("td", "snippet")):
            self.field = None

    def handle_data(self, data):
        if self.field and self.current is not None:
            text = self.current[self.field] + " " + data
            self.current[self.field] = " ".join(text.split())[:700]


def fetch(query):
    endpoint = "/lite/?" + urlencode({"q": query})
    connection = HTTPSConnection(HOST, timeout=8)
    try:
        connection.request("GET", endpoint, headers={"User-Agent": "Telegram-MCP-preset/1.0"})
        with connection.getresponse() as response:
            if response.status != 200:
                return [], f"Поиск временно недоступен (HTTP {response.status})."
            parser = Results()
            parser.feed(response.read(262_144).decode("utf-8", "replace"))
            if not parser.results:
                return [], "Поиск не вернул доступных результатов."
            return parser.results, ""
    except (OSError, HTTPException):
        return [], "Поиск временно недоступен."
    finally:
        connection.close()


def search(query):
    if not 0 < len(query) <= MAX_QUERY:
        return [], "Укажите тему после /web, до 400 символов."
    try:
        process = subprocess.run(
            [sys.executable, "-I", __file__], input=query, encoding="utf-8",
            capture_output=True, timeout=10, check=True,
        )
        return tuple(json.loads(process.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], "Поиск временно недоступен."


if __name__ == "__main__":
    print(json.dumps(fetch(sys.stdin.read(MAX_QUERY + 1)), ensure_ascii=False))
