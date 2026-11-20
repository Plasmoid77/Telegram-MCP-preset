"""Read-only web research using queries grounded in the owner's current DM."""

from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
import json
import re
import subprocess
import sys
from urllib.parse import parse_qs, urlencode, urlsplit


QUERY = re.compile(r"^(?:/web(?:\s+|$)|(?:поищи|найди|проверь)\s+в\s+интернете\b[\s:,.]*|"
                   r"(?:search|look up)\s+the\s+web\b[\s:,.]*)", re.IGNORECASE)
MAX_QUERY = 400
MAX_RESULTS = 6
HOST = "lite.duckduckgo.com"


def authored_text(message):
    if message.get("forwarded") or any(
        entity.get("type") in ("blockquote", "code", "pre")
        for entity in (message.get("formatting") or [])
    ):
        return ""
    return " ".join((message.get("text") or "").split())


def requested_query(message):
    """Return the literal query of an explicit search, or None."""
    text = authored_text(message)
    prefix = QUERY.match(text)
    return " ".join(text[prefix.end():].split()) if prefix else None


def query_from_messages(query, messages):
    """Send exactly the author's words, even when the model changes whitespace or case."""
    if not isinstance(query, str):
        return None
    query = " ".join(query.split())
    if not 0 < len(query) <= MAX_QUERY:
        return None
    pattern = re.compile(r"(?<!\w)" + re.escape(query) + r"(?!\w)", re.IGNORECASE)
    for message in messages:
        match = pattern.search(message)
        if match:
            return match[0]
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
