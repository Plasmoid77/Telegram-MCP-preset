"""The broker's web tools: public search (DuckDuckGo lite, no key) and opening a public link."""
import codecs
import ipaddress
import re
import socket
import ssl
import struct
import threading
import time
import zlib
from html.parser import HTMLParser
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

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


# --- open_url --------------------------------------------------------------------------------
MAX_BYTES = 3_000_000        # body read from a page
MAX_IMAGE = 5_000_000        # an image the model receives
MAX_TEXT = 200_000           # page text handed to the broker (it pages it further)
MAX_LINKS, MAX_IMAGES, MAX_REDIRECTS = 100, 30, 5
FETCH_SECONDS = 30
PORTS = {"http": 80, "https": 443}  # only the standard ports
ASCII_SAFE = "".join(chr(c) for c in range(33, 127))  # kept as is in a request target; the rest is %-encoded
MAX_PIXELS = 40_000_000                # an image larger than this is not passed to the model
IMAGE_TYPES = ("image/jpeg", "image/png", "image/gif", "image/webp")
TEXT_TYPES = ("text/plain", "text/markdown", "text/csv", "application/json", "application/xml", "text/xml")
HTML_TYPES = ("text/html", "application/xhtml+xml")
USER_AGENT = "Mozilla/5.0 (compatible; Telegram-MCP-preset/2; +https://github.com/Plasmoid77/Telegram-MCP-preset)"
# Text around links: links_in() splits on these and trims the punctuation that ends a sentence.
SEPARATORS = re.compile(r"[\s\"'<>«»\[\]{}|\\^`]+")
HOSTNAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})")
MAX_SCAN = 1_000_000          # characters of one text scanned for links


def split_url(text):
    """(scheme, host, port, path?query) of an http(s) link, or None. A link without a scheme is https."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text or len(text) > 2048 or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        url = urlsplit(text)
        port = url.port
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
        return None
    try:
        host = url.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    default = PORTS[url.scheme]
    if not host or (port is not None and port != default):
        return None
    target = quote((url.path or "/") + ("?" + url.query if url.query else ""), safe=ASCII_SAFE)
    return url.scheme, host, default, target


def link_key(text):
    """What makes two spellings of a link the same: host without www., path, query (no scheme,
    no fragment). None for anything that is not an http(s) link."""
    parts = split_url(text)
    if parts is None:
        return None
    host, target = parts[1], parts[3]
    host = host[4:] if host.startswith("www.") else host
    path, _, query = target.partition("?")
    path = path.rstrip("/") or "/"
    return host + path + ("?" + query if query else "")


def links_in(text):
    """{key: link} for every link written in a text, with or without a scheme (linear scan)."""
    found = {}
    for token in SEPARATORS.split((text or "")[:MAX_SCAN]):
        token = token.lstrip("(").rstrip(".,;:!?")
        while token.endswith(")") and token.count(")") > token.count("("):  # "(see x.org/a)" but "wiki/A_(b)"
            token = token[:-1].rstrip(".,;:!?")
        if "." not in token or len(token) > 2048:
            continue
        if token.startswith(("http://", "https://")) or (
                "://" not in token and "@" not in token.split("/", 1)[0]):
            parts = split_url(token)
            if parts and HOSTNAME.fullmatch(parts[1]):
                key = link_key(token)
                if key not in found or (parts[0] == "https" and found[key].startswith("http://")):
                    found[key] = absolute_url(parts)
    return found


def absolute_url(parts):
    """scheme://host/target from split_url() parts (ASCII, the form fetch() requests)."""
    scheme, host, port, target = parts
    netloc = "[" + host + "]" if ":" in host else host
    if port != {"http": 80, "https": 443}[scheme]:  # only in tests, which move PORTS
        netloc += f":{port}"
    return f"{scheme}://{netloc}{target}"


def nets(value):
    """Networks from a comma-separated config value (bad entries are ignored)."""
    out = []
    for item in (value or "").split(","):
        try:
            out.append(ipaddress.ip_network(item.strip(), strict=False))
        except ValueError:
            pass
    return out


def public(address, extra=()):
    """True for an address on the public internet (not loopback, LAN, link-local, CGNAT, ...).
    extra: networks to accept as well, e.g. a router's fake-ip range (198.18.0.0/15) whose
    addresses stand for public sites it reaches through a proxy."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if any(ip.version == n.version and ip in n for n in extra):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is not None:
            return public(str(inner))
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return public(str(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)))
    if ip.is_multicast or not ip.is_global or ip in SITE_LOCAL or assigned(ip):
        return False
    return not any(ip.version == n.version and ip in n for n in local_networks())


def assigned(ip):
    """True when the address belongs to this machine (a public address on one of its interfaces)."""
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.bind((str(ip), 0))
        return True
    except OSError:
        return False


SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # deprecated, but Python still calls it global


def local_networks():
    """Prefixes of this machine's own global IPv6 addresses (its LAN can be globally routed)."""
    out = []
    try:
        with open("/proc/net/if_inet6", encoding="ascii") as f:
            for line in f:
                fields = line.split()
                if len(fields) >= 4 and fields[3] == "00":  # scope: global
                    address = ipaddress.IPv6Address(int(fields[0], 16))
                    out.append(ipaddress.ip_network(f"{address}/{int(fields[2], 16)}", strict=False))
    except (OSError, ValueError):
        pass
    return out


def resolve(host, port, timeout):
    """Every address of a host, in the resolver's order; the lookup is abandoned after timeout."""
    box = []

    def lookup():
        try:
            box.append(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except (OSError, UnicodeError) as e:
            box.append(e)

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(max(0.1, timeout))
    if not box or isinstance(box[0], Exception):
        raise OSError("lookup failed")
    out = []
    for info in box[0]:
        if info[4][0] not in out:
            out.append(info[4][0])
    return out


def connect(addresses, port, timeout, deadline):
    """A socket to the first checked address that answers (no second DNS lookup to rebind)."""
    error = OSError("no address")
    for address in addresses:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            return socket.create_connection((address, port), min(timeout, left))
        except OSError as e:
            error = e
    raise error


class _Pinned(HTTPConnection):
    def __init__(self, host, port, addresses, timeout, deadline):
        super().__init__(host, port, timeout=timeout)
        self.addresses, self.deadline = addresses, deadline

    live = None  # the socket in use, kept for the watchdog (http.client may drop self.sock while reading)

    def connect(self):
        self.sock = self.live = connect(self.addresses, self.port, self.timeout, self.deadline)

    def abort(self):
        """Called by the watchdog: wakes up a handshake or read that is still waiting."""
        sock = self.live
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class _PinnedTLS(_Pinned, HTTPSConnection):
    """The certificate is verified against the host name, as usual."""

    def __init__(self, host, port, addresses, timeout, deadline):
        HTTPSConnection.__init__(self, host, port, timeout=timeout, context=ssl.create_default_context())
        self.addresses, self.deadline = addresses, deadline

    def connect(self):
        self.live = connect(self.addresses, self.port, self.timeout, self.deadline)
        self.sock = self.live = self._context.wrap_socket(self.live, server_hostname=self.host)


class Page(HTMLParser):
    """Readable text, links and images of an HTML page."""
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "object", "canvas", "select"}
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article", "header", "footer",
             "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dd", "dt", "figcaption", "hr", "main",
             "nav", "aside", "form", "td", "th"}

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.title, self.links, self.images = base, [], "", [], []
        self.skip, self.in_title, self.link = 0, False, None

    def absolute(self, value):
        try:
            url = urljoin(self.base, (value or "").strip())
        except ValueError:
            return None
        return url if split_url(url) else None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag == "a":
            url = self.absolute(attrs.get("href"))
            self.link = [url, ""] if url and len(self.links) < MAX_LINKS else None
        elif tag == "img" or (tag == "meta" and (attrs.get("property") or attrs.get("name")) in ("og:image", "twitter:image")):
            url = self.absolute(attrs.get("content") if tag == "meta" else attrs.get("src") or attrs.get("data-src"))
            if url and url not in self.images and len(self.images) < MAX_IMAGES:
                self.images.append(url)
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self.in_title = False
        elif tag == "a" and self.link:
            url, label = self.link
            if all(u != url for _, u in self.links):
                self.links.append((" ".join(label.split())[:120], url))
            self.link = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            if len(self.title) < 1000:
                self.title += data[:1000]
        elif not self.skip:
            self.parts.append(data)
            if self.link is not None and len(self.link[1]) < 500:
                self.link[1] += data[:500]

    def text(self):
        out = []
        for line in "".join(self.parts).splitlines():
            line = " ".join(line.split())
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def charset(content_type, body, html):
    match = re.search(r"charset=[\"']?([\w.:-]+)", content_type, re.IGNORECASE)
    name = match.group(1) if match else None
    if not name and html:
        meta = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", body[:4096], re.IGNORECASE)
        name = meta.group(1).decode("ascii", "replace") if meta else None
    try:
        info = codecs.lookup(name or "utf-8")
    except LookupError:
        return "utf-8"
    return info.name if getattr(info, "_is_text_encoding", True) else "utf-8"


def read_body(response, limit, deadline):
    """Up to limit + 1 bytes (one more shows the body was cut), within the overall deadline."""
    chunks, size = [], 0
    while size <= limit:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read1(min(65536, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if time.monotonic() > deadline or (size <= limit and response.length):  # cut by the watchdog / short
        raise TimeoutError
    return b"".join(chunks)


def image_size(data, kind):
    """(width, height) from an image header, or None when the bytes are not that image type."""
    try:
        if kind == "image/png" and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return struct.unpack(">II", data[16:24])
        if kind == "image/gif" and data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if kind == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            if chunk == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return (w & 0x3FFF, h & 0x3FFF)
        if kind == "image/jpeg" and data[:2] == b"\xff\xd8":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker, length = data[i + 1], struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return (w, h)
                i += 2 + length
    except struct.error:
        return None
    return None


def fetch(url, timeout=15, extra=()):
    """Open a public http(s) link. Returns (result, error). result for a page or text:
    {"url", "type", "title", "text", "links": [(label, url)], "images": [url]}; for an image:
    {"url", "type", "image": bytes}. Every hop (redirects too) connects only to public addresses
    (or ones in extra); a host with none of them is refused. The whole call ends within
    FETCH_SECONDS: a watchdog closes the socket of a site that sends slowly."""
    deadline = time.monotonic() + FETCH_SECONDS
    parts = split_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        if parts is None:
            return None, "Only http(s) links on the standard ports can be opened."
        scheme, host, port, target = parts
        url = absolute_url(parts)
        left = deadline - time.monotonic()
        if left <= 0:
            return None, "The site did not answer in time."
        try:
            addresses = resolve(host, port, min(timeout, left))
        except OSError:
            return None, "The site's name does not resolve."
        addresses = [a for a in addresses if public(a, extra)]
        if not addresses:
            return None, "This address is not on the public internet; it cannot be opened."
        connection = (_PinnedTLS if scheme == "https" else _Pinned)(host, port, addresses, timeout, deadline)
        watchdog = threading.Timer(max(0.1, deadline - time.monotonic()), connection.abort)
        watchdog.start()
        try:
            connection.request("GET", target, headers={
                "User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8", "Accept-Encoding": "identity",
                "Accept": "text/html,application/xhtml+xml,text/plain,application/json,image/*;q=0.8,*/*;q=0.5"})
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = (response.getheader("Location") or "").strip()
                    try:
                        parts = split_url(urljoin(url, location)) if location else None
                    except ValueError:
                        parts = None
                    if parts is None:
                        return None, f"The site answered HTTP {response.status} with no usable target."
                    if scheme == "https" and parts[0] == "http":
                        return None, "The site redirects from https to plain http; it was not followed."
                    continue
                if response.status != 200:
                    return None, f"The site answered HTTP {response.status}."
                content_type = response.getheader("Content-Type") or ""
                kind = content_type.split(";", 1)[0].strip().lower()
                encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
                if encoding not in ("identity", "gzip", "deflate"):
                    return None, "The site sent the page in an unsupported encoding."
                if kind in IMAGE_TYPES:
                    limit = MAX_IMAGE
                elif kind in HTML_TYPES or kind in TEXT_TYPES or kind.startswith("text/"):
                    limit = MAX_BYTES
                else:
                    return None, "This link is not a web page, text or a picture (e.g. a file or a video)."
                body = read_body(response, limit, deadline)
        except ssl.SSLError:
            return None, "The site's TLS certificate is not valid; it was not opened."
        except (OSError, HTTPException, ValueError, AttributeError):  # AttributeError: closed by the watchdog
            return None, "The site did not answer in time or closed the connection."
        finally:
            watchdog.cancel()
            connection.close()
        if encoding != "identity":
            inflater = zlib.decompressobj(zlib.MAX_WBITS | 32)
            try:
                body = inflater.decompress(body, limit + 1)
            except zlib.error:
                return None, "The site sent a broken compressed page."
            if not inflater.eof and len(body) <= limit:
                return None, "The site sent an incomplete compressed page."
        if kind in IMAGE_TYPES:
            if len(body) > limit:
                return None, "The image is too large."
            size = image_size(body, kind)
            if size is None:
                return None, "The image is damaged or not of the type the site claims."
            if size[0] * size[1] > MAX_PIXELS:
                return None, f"The image is too large ({size[0]}x{size[1]})."
            return {"url": url, "type": kind, "image": body}, ""
        body = body[:limit]
        html = kind in HTML_TYPES
        try:
            text = body.decode(charset(content_type, body, html), "replace")
        except (UnicodeError, LookupError, TypeError):
            text = body.decode("utf-8", "replace")
        if not html:
            return {"url": url, "type": kind, "title": "", "text": text[:MAX_TEXT], "links": [], "images": []}, ""
        page = Page(url)
        try:
            page.feed(text)
            page.close()
        except Exception:  # markup the parser cannot handle: keep what it read
            pass
        return {"url": url, "type": kind, "title": " ".join(page.title.split())[:300],
                "text": page.text()[:MAX_TEXT], "links": page.links, "images": page.images}, ""
    return None, "Too many redirects."
