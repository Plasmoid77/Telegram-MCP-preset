"""HTTP layer of asr/asr_server.py (OpenAI-compatible /v1/audio/transcriptions).

Recognition is stubbed, so neither onnx-asr nor a model is needed.
Run: python3 -m unittest discover -s tests -v
"""
import importlib.util, json, threading, unittest, urllib.error, urllib.request, uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "asr_server", Path(__file__).resolve().parent.parent / "asr" / "asr_server.py")
asr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(asr)


def multipart(fields):
    """fields: name -> str | (filename, bytes, mime). Returns (body, content_type)."""
    b = uuid.uuid4().hex
    out = []
    for name, v in fields.items():
        if isinstance(v, tuple):
            fn, data, mime = v
            out.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                       f"Content-Type: {mime}\r\n\r\n".encode() + data + b"\r\n")
        else:
            out.append(f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{v}\r\n'.encode())
    out.append(f"--{b}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={b}"


class AsrHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []

        def fake(data, language=None):
            cls.calls.append((data, language))
            return "Привет, мир.", 3.5

        asr.transcribe = fake
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), asr.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def post(self, fields, path="/v1/audio/transcriptions"):
        body, ctype = multipart(fields)
        req = urllib.request.Request(self.url + path, body, {"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Type"), r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read().decode()

    def test_json_like_telegram_mcp_sends_it(self):
        voice = b"OggS\x00fake-opus-bytes\r\n--not-a-boundary"
        code, ctype, body = self.post({"model": "gigaam-v3-e2e-rnnt", "response_format": "json",
                                       "language": "ru", "file": ("voice.ogg", voice, "audio/ogg")})
        self.assertEqual(code, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body), {"text": "Привет, мир."})
        self.assertEqual(self.calls[-1], (voice, "ru"))

    def test_text_and_verbose_json(self):
        f = ("v.ogg", b"x", "audio/ogg")
        code, ctype, body = self.post({"file": f, "response_format": "text"})
        self.assertEqual((code, body), (200, "Привет, мир."))
        self.assertIn("text/plain", ctype)
        code, _, body = self.post({"file": f, "response_format": "verbose_json"})
        self.assertEqual(json.loads(body)["duration"], 3.5)

    def test_errors(self):
        code, _, body = self.post({"model": "x"})
        self.assertEqual(code, 400)
        self.assertIn("file", json.loads(body)["error"]["message"])
        code, _, _ = self.post({"file": ("v.ogg", b"x", "audio/ogg")}, path="/v1/other")
        self.assertEqual(code, 404)

    def test_health(self):
        with urllib.request.urlopen(self.url + "/health") as r:
            d = json.loads(r.read())
        self.assertEqual(d["data"][0]["id"], asr.MODEL)


if __name__ == "__main__":
    unittest.main()
