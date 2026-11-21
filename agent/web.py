"""Public web search (DuckDuckGo lite, no key) for the broker's search_web tool."""
import re
from html.parser import HTMLParser
from http.client import HTTPException, HTTPSConnection
from urllib.parse import parse_qs, urlencode, urlsplit

HOST = "lite.duckduckgo.com"
MAX_QUERY = 300
MAX_RESULTS = 6
# Things that must never leave in a query: @handles, links, IPs, long numbers and phones,
# common key prefixes, "password: ..." and control characters. A filter cannot recognise
# arbitrary private prose; the prompt and the broker's rules cover the rest.
PRIVATE = re.compile(
    r"@\w|\b(?:https?|file)://|\b(?:t\.me|telegram\.me)/|"
    r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)|\b\d{9,}\b|\+\d[\d .()-]{8,}\d|"
    r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[a-z0-9_-]{8,}|"
    r"\b(?:password|passwd|пароль|secret|api[_ -]?key|token)\s*[:=]\s*\S|[\x00-\x1f\x7f]",
    re.IGNORECASE)


def normalize(query):
    if not isinstance(query, str):
        return ""
    query = " ".join(query.split())
    return query if 0 < len(query) <= MAX_QUERY else ""


def private(query, identifiers=()):
    """True when the query holds an identifier or looks like private data."""
    return bool(PRIVATE.search(query)) or any(
        re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", query, re.IGNORECASE) for value in identifiers if value)


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
        self.results, self.current, self.field = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if tag == "a" and "result-link" in classes:
            href = result_url(attrs.get("href") or "")
            self.current = {"url": href, "title": "", "snippet": ""} if href else None
            if self.current is not None and len(self.results) < MAX_RESULTS:
                self.results.append(self.current)
                self.field = "title"
        elif tag == "td" and "result-snippet" in classes and self.current is not None:
            self.field = "snippet"

    def handle_endtag(self, tag):
        if (tag, self.field) in (("a", "title"), ("td", "snippet")):
            self.field = None

    def handle_data(self, data):
        if self.field and self.current is not None:
            self.current[self.field] = " ".join((self.current[self.field] + " " + data).split())[:700]


def search(query, timeout=10):
    """(results, error): up to MAX_RESULTS {url, title, snippet}, or an error text."""
    connection = HTTPSConnection(HOST, timeout=timeout)
    try:
        connection.request("GET", "/lite/?" + urlencode({"q": query}),
                           headers={"User-Agent": "Telegram-MCP-preset/2"})
        with connection.getresponse() as response:
            if response.status != 200:
                return [], f"Search is unavailable right now (HTTP {response.status})."
            parser = Results()
            parser.feed(response.read(262_144).decode("utf-8", "replace"))
        return (parser.results, "") if parser.results else ([], "No results.")
    except (OSError, HTTPException):
        return [], "Search is unavailable right now."
    finally:
        connection.close()
