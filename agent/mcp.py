"""Minimal streamable-HTTP MCP client (stdlib only) for the upstream telegram-mcp server."""
import json
import urllib.error
import urllib.request

PROTOCOL = "2025-06-18"


class Mcp:
    def __init__(self, url):
        self.url, self.sid, self.n = url, None, 0

    def _post(self, payload, timeout):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": PROTOCOL}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, json.dumps(payload).encode(), headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            ctype = r.headers.get("Content-Type", "")
            body = r.read().decode("utf-8", "replace")
        if "id" not in payload:
            return None
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if msg.get("id") == payload["id"]:
                        return msg
            raise RuntimeError("no response in event stream")
        return json.loads(body)

    def _connect(self):
        self.sid = None
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "telegram-agent", "version": "2"}}}, 30)
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30)

    def rpc(self, method, params, timeout):
        if not self.sid:
            self._connect()
        self.n += 1
        payload = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}
        try:
            resp = self._post(payload, timeout)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404):  # 404: the session expired (server restart)
                raise
            self._connect()
            resp = self._post(payload, timeout)
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp["result"]

    def catalog(self):
        """Every tool the server offers (all pages of tools/list)."""
        tools, params = [], {}
        while True:
            page = self.rpc("tools/list", params, 30)
            tools.extend(page["tools"])
            if not page.get("nextCursor"):
                return tools
            params = {"cursor": page["nextCursor"]}

    def tool_result(self, name, args, timeout=60):
        """The raw MCP tool result ({"content": [...], "isError": bool})."""
        return self.rpc("tools/call", {"name": name, "arguments": args}, timeout)

    def call(self, name, args, timeout=60, raw=False):
        """A tool's text, parsed as JSON unless raw. A tool error raises RuntimeError."""
        result = self.tool_result(name, args, timeout)
        text = "".join(c.get("text", "") for c in result.get("content", []))
        if result.get("isError"):
            raise RuntimeError(f"{name}: {text[:300]}")
        if raw:
            return text
        try:
            return json.loads(text)
        except ValueError:
            raise RuntimeError(f"{name}: {text[:300]}") from None
