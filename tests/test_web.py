"""web.py: links a run may open, public addresses, and fetch() against a local server."""
import gzip
import ipaddress
import struct
import time
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
import web  # noqa: E402

LOCAL = [ipaddress.ip_network("127.0.0.0/8")]
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 2, 3) + b"0" * 100
HUGE = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 20000, 20000) + b"0" * 100


class Links(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(web.link_key("HTTPS://www.Example.com:443/a/?x=1#top"), "example.com/a?x=1")
        self.assertEqual(web.link_key("example.com"), web.link_key("http://example.com/"))
        self.assertNotEqual(web.link_key("example.com/a?x=1"), web.link_key("example.com/a?x=2"))
        for bad in ("ftp://example.com/", "file:///etc/passwd", "http://user:pw@example.com/",
                    "http://example.com:8080/", "javascript:alert(1)", "", None, "http://exa mple.com/"):
            self.assertIsNone(web.link_key(bad), bad)

    def test_links_in_text(self):
        found = web.links_in('см. https://Example.com/a/?x=1. и (avito.ru), «docs.python.org/3/» '
                             '{"url": "https://t.me/c/1/2"} пример.рф/путь mail me@mail.example '
                             'ftp://files.example/x http://plain.example/p https://plain.example/p '
                             '(см. en.wikipedia.org/wiki/S.M.A.R.T.) en.wikipedia.org/wiki/Foo_(bar)')
        self.assertEqual(set(found), {"example.com/a?x=1", "avito.ru/", "docs.python.org/3", "t.me/c/1/2",
                                      web.link_key("пример.рф/путь"), "plain.example/p",
                                      "en.wikipedia.org/wiki/S.M.A.R.T", "en.wikipedia.org/wiki/Foo_(bar)"})
        self.assertEqual(found["example.com/a?x=1"], "https://example.com/a/?x=1")  # fetched as written
        self.assertEqual(found["avito.ru/"], "https://avito.ru/")
        self.assertEqual(found["plain.example/p"], "https://plain.example/p")      # https wins
        self.assertTrue(found[web.link_key("пример.рф/путь")].startswith("https://xn--e1afmkfd.xn--p1ai/%D0"))

    def test_scan_is_linear(self):
        started = time.monotonic()
        web.links_in("a" * 500_000 + " " + "b." * 200_000)
        self.assertLess(time.monotonic() - started, 2)


class Addresses(unittest.TestCase):
    def test_public(self):
        for a in ("8.8.8.8", "151.101.0.223", "2a00:1450:4010::64"):
            self.assertTrue(web.public(a), a)
        for a in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "172.16.0.1", "169.254.169.254", "100.64.0.1",
                  "0.0.0.0", "224.0.0.1", "198.18.0.5", "::1", "fe80::1", "fc00::1", "fec0::1", "::ffff:127.0.0.1",
                  "::ffff:10.0.0.1", "2002:0a00:0001::1", "64:ff9b::a00:1", "ff02::1", "not-an-ip"):
            self.assertFalse(web.public(a), a)

    def test_own_addresses(self):
        self.assertTrue(web.assigned(ipaddress.ip_address("127.0.0.1")))
        self.assertFalse(web.assigned(ipaddress.ip_address("8.8.8.8")))

    def test_extra_networks(self):
        extra = web.nets("198.18.0.0/15, junk")
        self.assertEqual(len(extra), 1)
        self.assertTrue(web.public("198.18.0.99", extra))
        self.assertFalse(web.public("fc00::5b", extra))

    def test_own_ipv6_network_is_not_public(self):
        own = [ipaddress.ip_network("2a00:1111:2222:3333::/64")]
        with mock.patch.object(web, "local_networks", return_value=own):
            self.assertFalse(web.public("2a00:1111:2222:3333::1"))
            self.assertTrue(web.public("2a00:1111:2222:4444::1"))


class Site(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        port = self.server.server_port
        routes = {
            "/page": lambda: self.send(200, (
                "<html><head><title> Заголовок </title><meta property='og:image' content='/og.png'>"
                "<style>.x{}</style><script>alert('steal')</script></head><body><h1>Привет</h1>"
                "<p>Текст <a href='/next?a=1'>дальше</a> и <a href='https://example.org/x'>там</a></p>"
                "<img src='img/1.jpg'><noscript>no</noscript></body></html>").encode()),
            "/cp1251": lambda: self.send(200, "<meta charset='windows-1251'><p>Кириллица</p>".encode("cp1251"),
                                         ctype="text/html"),
            "/gz": lambda: self.send(200, gzip.compress(b"plain gzip"), ctype="text/plain",
                                     headers=[("Content-Encoding", "gzip")]),
            "/img": lambda: self.send(200, PNG, ctype="image/png"),
            "/huge": lambda: self.send(200, HUGE, ctype="image/png"),
            "/fake": lambda: self.send(200, b"<html>", ctype="image/png"),
            "/cut": lambda: self.send(200, gzip.compress(b"x" * 1000)[:20], ctype="text/plain",
                                      headers=[("Content-Encoding", "gzip")]),
            "/b64": lambda: self.send(200, b"text", ctype="text/plain; charset=base64"),
            "/idna": lambda: self.send(200, b"\xff text", ctype="text/plain; charset=idna"),
            "/badloc": lambda: self.send(302, headers=[("Location", "http://[::1")]),
            "/slow": self.slow,
            "/pdf": lambda: self.send(200, b"%PDF", ctype="application/pdf"),
            "/missing": lambda: self.send(404, b"no"),
            "/hop": lambda: self.send(302, headers=[("Location", "/page")]),
            "/loop": lambda: self.send(302, headers=[("Location", "/loop")]),
            "/inside": lambda: self.send(302, headers=[("Location", f"http://10.0.0.1:{port}/page")]),
        }
        routes.get(self.path.split("?")[0], lambda: self.send(404))()

    def slow(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "100")
        self.end_headers()
        try:
            for _ in range(100):
                self.wfile.write(b"x")
                self.wfile.flush()
                time.sleep(0.2)
        except OSError:
            pass


class Fetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.ports = mock.patch.dict(web.PORTS, {"http": cls.server.server_port})
        cls.ports.start()

    @classmethod
    def tearDownClass(cls):
        cls.ports.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path, extra=LOCAL):
        return web.fetch(self.base + path, timeout=5, extra=extra)

    def test_html_page(self):
        page, error = self.get("/page")
        self.assertEqual(error, "")
        self.assertEqual(page["title"], "Заголовок")
        self.assertIn("Привет", page["text"])
        self.assertIn("Текст дальше и там", page["text"])
        self.assertNotIn("steal", page["text"])
        self.assertNotIn("no", page["text"].split())
        self.assertIn(("дальше", self.base + "/next?a=1"), page["links"])
        self.assertIn(("там", "https://example.org/x"), page["links"])
        self.assertEqual(page["images"], [self.base + "/og.png", self.base + "/img/1.jpg"])

    def test_charset_gzip_image(self):
        self.assertIn("Кириллица", self.get("/cp1251")[0]["text"])
        self.assertEqual(self.get("/gz")[0]["text"], "plain gzip")
        image, error = self.get("/img")
        self.assertEqual((image["type"], image["image"], error), ("image/png", PNG, ""))
        with mock.patch.object(web, "MAX_IMAGE", 10):
            self.assertEqual(self.get("/img"), (None, "The image is too large."))
        self.assertIn("20000x20000", self.get("/huge")[1])
        self.assertIn("damaged", self.get("/fake")[1])
        self.assertIn("incomplete", self.get("/cut")[1])
        self.assertEqual(self.get("/b64")[0]["text"], "text")
        self.assertIn("text", self.get("/idna")[0]["text"])

    def test_slow_site_is_cut_at_the_deadline(self):
        started = time.monotonic()
        with mock.patch.object(web, "FETCH_SECONDS", 1):
            page, error = self.get("/slow")
        self.assertIsNone(page)
        self.assertIn("did not answer in time", error)
        self.assertLess(time.monotonic() - started, 3)

    def test_redirects(self):
        self.assertEqual(self.get("/hop")[0]["url"], self.base + "/page")
        self.assertEqual(self.get("/loop"), (None, "Too many redirects."))
        self.assertIn("not on the public internet", self.get("/inside")[1])
        self.assertIn("no usable target", self.get("/badloc")[1])

    def test_refusals(self):
        self.assertIn("not on the public internet", self.get("/page", extra=())[1])
        self.assertIn("HTTP 404", self.get("/missing")[1])
        self.assertIn("not a web page", self.get("/pdf")[1])
        self.assertIn("standard ports", web.fetch("http://127.0.0.1:1/", extra=LOCAL)[1])


if __name__ == "__main__":
    unittest.main()
