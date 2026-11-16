"""Local speech-to-text for telegram-mcp: a thin OpenAI-compatible /v1/audio/transcriptions
endpoint over onnx-asr (https://github.com/istupakov/onnx-asr, MIT).

Any onnx-asr model works (ASR_MODEL): GigaAM v2/v3 (default gigaam-v3-e2e-rnnt, Russian with
punctuation), Parakeet, Canary, Whisper, Vosk, T-one, or a Hugging Face repo in onnx-asr format.
Audio of any format and length: ffmpeg decodes it to 16 kHz mono, onnx-asr's Silero VAD cuts
long recordings at pauses (models take ~20-30 s per pass).

telegram-mcp's `openai` transcription engine posts the voice note here (multipart: file,
model, response_format, language) and reads {"text": ...}. The model is loaded on the first
request and unloaded after ASR_IDLE_UNLOAD seconds without requests.

Part of Telegram-MCP-preset: https://github.com/Plasmoid77/Telegram-MCP-preset
"""
import gc, json, os, subprocess, sys, threading, time
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("ASR_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASR_PORT", "5093"))
MODEL = os.environ.get("ASR_MODEL", "gigaam-v3-e2e-rnnt")
QUANT = os.environ.get("ASR_QUANTIZATION", "int8") or None   # "" = full precision
THREADS = int(os.environ.get("ASR_THREADS", "4"))
IDLE_UNLOAD = int(os.environ.get("ASR_IDLE_UNLOAD", "600"))
MAX_BYTES = int(float(os.environ.get("ASR_MAX_MB", "25")) * 1048576)
MAX_SECONDS = int(os.environ.get("ASR_MAX_SECONDS", "600"))  # longer audio is cut (bounds RAM/CPU)
DECODE_TIMEOUT = 120
SHORT_S = 20.0           # up to this length the model takes the audio in one pass
SR = 16000

_lock = threading.Lock()  # one decode + recognition at a time (CPU and RAM bound)
_state = {"asr": None, "vad": None, "last": 0.0}


def log(*a):
    print(time.strftime("%F %T"), *a, file=sys.stderr, flush=True)


def _models():
    if _state["asr"] is None:
        import onnxruntime as ort, onnx_asr
        so = ort.SessionOptions()
        so.intra_op_num_threads = THREADS
        so.inter_op_num_threads = 1
        t0 = time.time()
        asr = onnx_asr.load_model(MODEL, quantization=QUANT, sess_options=so)
        vad = asr.with_vad(onnx_asr.load_vad("silero", sess_options=so))
        _state["asr"], _state["vad"] = asr, vad  # publish only when both loaded
        log(f"loaded {MODEL} ({QUANT or 'full precision'}) in {time.time() - t0:.1f}s")
    _state["last"] = time.monotonic()
    return _state["asr"], _state["vad"]


def _unloader():
    while True:
        time.sleep(30)
        with _lock:
            if _state["asr"] is not None and time.monotonic() - _state["last"] > IDLE_UNLOAD:
                _state["asr"] = _state["vad"] = None
                gc.collect()
                log("model unloaded (idle)")


def decode(data):
    """Any audio ffmpeg understands -> 16 kHz mono float32 array, at most MAX_SECONDS long."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-t", str(MAX_SECONDS),
                          "-f", "s16le", "-ac", "1", "-ar", str(SR), "pipe:1"],
                         input=data, capture_output=True, check=True, timeout=DECODE_TIMEOUT).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(data, language=None):
    kw = {"language": language} if language and "whisper" in MODEL.lower() else {}
    with _lock:
        wav = decode(data)
        asr, vad = _models()
        if len(wav) <= SHORT_S * SR:
            text = asr.recognize(wav, sample_rate=SR, **kw)
        else:
            text = " ".join(s.text.strip() for s in vad.recognize(wav, sample_rate=SR, **kw) if s.text.strip())
        _state["last"] = time.monotonic()
    return text.strip(), len(wav) / SR


class Handler(BaseHTTPRequestHandler):
    server_version = "asr-server/1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/v1/models"):
            return self._json(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}],
                                    "loaded": _state["asr"] is not None})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/audio/transcriptions":
            return self._json(404, {"error": {"message": "not found"}})
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 < size <= MAX_BYTES + 65536:
            return self._json(413, {"error": {"message": f"body must be 1..{MAX_BYTES} bytes"}})
        body = self.rfile.read(size)
        msg = BytesParser(policy=HTTP).parsebytes(
            b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + body)
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()} \
            if msg.is_multipart() else {}
        field = lambda k, d="": parts[k].get_content().strip() if k in parts else d
        if "file" not in parts:
            return self._json(400, {"error": {"message": "multipart field 'file' is required"}})
        fmt = field("response_format", "json")
        t0 = time.time()
        try:
            text, dur = transcribe(parts["file"].get_payload(decode=True), field("language") or None)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return self._json(400, {"error": {"message": "could not decode audio"}})
        except Exception as e:  # keep serving
            log(f"error: {e!r}")
            return self._json(500, {"error": {"message": str(e)}})
        log(f"{dur:.1f}s audio -> {len(text)} chars in {time.time() - t0:.1f}s")
        if fmt == "text":
            return self._send(200, text.encode(), "text/plain; charset=utf-8")
        out = {"text": text}
        if fmt == "verbose_json":
            out.update(duration=dur, language=field("language") or None)
        self._json(200, out)

    def log_message(self, *a):  # requests are logged in do_POST
        pass


if __name__ == "__main__":
    threading.Thread(target=_unloader, daemon=True).start()
    log(f"asr-server on http://{HOST}:{PORT}/v1 model={MODEL} quantization={QUANT} threads={THREADS}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
