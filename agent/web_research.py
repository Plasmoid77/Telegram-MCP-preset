"""Read-only search for public web sources."""

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
FIRECRAWL_HOST = "api.firecrawl.dev"
DASHES = str.maketrans("‐‑‒–—−", "------")
MODEL_ID = re.compile(r"[a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*")
PRIVATE_QUERY = re.compile(
    r"@|\b(?:https?|file)://|\b(?:t\.me|telegram\.me)/|"
    r"(?<!\d)\d{1,3}(?:\.\d{1,3}){3}(?!\d)|"
    r"\b\d{9,}\b|\+\d[\d .()-]{8,}\d|"
    r"\b\d{1,3}[ -]*(?:\(\d{2,4}\)|[ -]\d{2,4})[ -]*\d{3,4}[ -]\d{2,4}[ -]\d{2,4}\b|"
    r"\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[a-z0-9_-]{8,}|"
    r"\b(?:password|passwd|пароль|secret|api[_ -]?key|token)\s*[:=]\s*\S|"
    r"[\x00-\x1f\x7f]",
    re.IGNORECASE,
)


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


def normalize_query(query):
    if not isinstance(query, str):
        return ""
    query = " ".join(query.split())
    return query if len(query) <= MAX_QUERY else ""


def contains_private_identifier(query, identifiers=()):
    if PRIVATE_QUERY.search(query):
        return True
    return any(re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", query, re.IGNORECASE)
               for value in identifiers if value)


def result_url(href):
    if not isinstance(href, str):
        return ""
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


def fetch(query, *, timeout=8):
    endpoint = "/lite/?" + urlencode({"q": query})
    connection = HTTPSConnection(HOST, timeout=timeout)
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


def fetch_firecrawl(query):
    connection = HTTPSConnection(FIRECRAWL_HOST, timeout=5)
    try:
        payload = json.dumps({"query": query, "limit": MAX_RESULTS, "sources": [{"type": "web"}]})
        connection.request("POST", "/v2/search", payload, {"Content-Type": "application/json"})
        with connection.getresponse() as response:
            if response.status != 200:
                return [], f"Поиск временно недоступен (HTTP {response.status})."
            data = json.loads(response.read(262_144))
        rows = data["data"]["web"]
        if data.get("success") is not True or not isinstance(rows, list):
            return [], "Поисковик вернул неожиданный ответ."
        results = []
        for row in rows[:MAX_RESULTS]:
            url = result_url(row.get("url")) if isinstance(row, dict) else ""
            if url:
                results.append({"url": url, "title": " ".join(str(row.get("title") or "").split())[:700],
                                "snippet": " ".join(str(row.get("description") or "").split())[:700]})
        return (results, "") if results else ([], "Поиск не вернул доступных результатов.")
    except (OSError, HTTPException, KeyError, TypeError, ValueError):
        return [], "Поиск временно недоступен."
    finally:
        connection.close()


def fetch_sources(query, fallback):
    results, error = fetch(query, timeout=4 if fallback else 8)
    if not error or not fallback:
        return results, error
    results, fallback_error = fetch_firecrawl(query)
    return (results, "") if not fallback_error else ([], f"DuckDuckGo: {error} Firecrawl: {fallback_error}")


def search(query, *, fallback=False):
    if not 0 < len(query) <= MAX_QUERY:
        return [], "Укажите тему после /web, до 400 символов."
    if contains_private_identifier(query):
        return [], "Запрос содержит идентификатор или приватные данные."
    try:
        process = subprocess.run(
            [sys.executable, "-I", __file__, "firecrawl" if fallback else ""], input=query, encoding="utf-8",
            capture_output=True, timeout=10, check=True,
        )
        return tuple(json.loads(process.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return [], "Поиск временно недоступен."


if __name__ == "__main__":
    query = sys.stdin.read(MAX_QUERY + 1)
    print(json.dumps(fetch_sources(query, fallback=sys.argv[1:] == ["firecrawl"]), ensure_ascii=False))
