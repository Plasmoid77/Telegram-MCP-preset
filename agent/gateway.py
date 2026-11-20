"""Per-run Streamable HTTP MCP gateway. Telegram access is checked before forwarding."""

import json
import secrets
import threading
from tempfile import NamedTemporaryFile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import web_research

GUEST_CHAT_TOOLS = {
    "get_history", "get_messages_after", "get_message_context", "get_messages",
    "list_messages", "get_chat", "send_reaction", "mark_as_read", "send_typing",
    "open_photo", "get_media_info", "inspect_document", "transcribe_voice", "view_video",
}
GUEST_TOOLS = GUEST_CHAT_TOOLS | {
    "get_me", "get_user_status", "send_message", "reply_to_message", "forward_message",
}
BLOCKED_TOOLS = {"disable_incoming_feed", "wait_for_settled_message"}
READ_ONLY_TELEGRAM = {
    "get_me", "get_history", "get_messages_after", "get_message_context", "get_messages",
    "list_messages", "get_chat", "get_user_status", "list_chats", "search_messages", "search_global",
    "resolve_username", "list_topics", "list_folders", "get_folder", "get_message_link",
}
CHAT_STATUS_TOOLS = {"mark_as_read", "send_typing"}
OWNER_DIRECT_TOOLS = GUEST_TOOLS | {
    "list_chats", "search_messages", "search_global", "resolve_username",
    "list_topics", "list_folders", "get_folder", "create_folder", "delete_folder",
    "add_chat_to_folder", "remove_chat_from_folder", "get_message_link",
    "send_file", "export_chat_history",
}
TEXT_PAGE = 16_000
MAX_MEMORY = 64_000
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")


def tool(name, description, properties):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False,
        },
    }


READ_RESULT = tool("read_result", "Read the next page of a long Telegram tool result.",
                   {"result_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}})
MEMORY_READ = tool("memory_read", "Read the owner's persistent instructions.", {})
MEMORY_WRITE = tool("memory_write", "Replace the owner's persistent instructions with this text.",
                    {"text": {"type": "string", "maxLength": MAX_MEMORY}})

FIND_TOOL = tool("find_telegram_tool", "Find additional Telegram tools and their exact input schemas.",
                 {"query": {"type": "string", "description": "Part of a tool name, such as 'folder'."}})
CALL_TOOL = tool("call_telegram_tool", "Call an additional Telegram tool using its exact input schema.",
                 {"name": {"type": "string"}, "arguments": {"type": "object"}})
WEB_SEARCH = tool("search_web", "Search the public web for current facts or sources. Use a short "
                  "public phrase copied from the owner's current messages. For the configured public "
                  "model, use @model if the run instructions say it is available. "
                  "After searching, reply with links using send_message in this chat.",
                  {"query": {"type": "string", "maxLength": web_research.MAX_QUERY}})


def normalize(value):
    return str(value).strip().lower().lstrip("@")


def guest_arguments(name, args, chat, notify):
    if args.get("account") is not None:
        return None
    if name == "transcribe_voice" and args.get("engine") not in (None, ""):
        return None
    if name == "open_photo" and args.get("save_path") is not None:
        return None
    if name == "get_me":
        return {}
    if name in {"send_message", "reply_to_message"}:
        target = normalize(args.get("chat_id"))
        if target == str(chat):
            return dict(args, chat_id=chat)
        if target in notify:
            return dict(args, chat_id=notify[target])
        return None
    if name == "forward_message":
        target = normalize(args.get("to_chat_id"))
        if (normalize(args.get("from_chat_id")) == str(chat) and target in notify
                and args.get("send_as") is None):
            return dict(args, from_chat_id=chat, to_chat_id=notify[target])
        return None
    key = "user_id" if name == "get_user_status" else "chat_id"
    if (name in GUEST_CHAT_TOOLS or name == "get_user_status") and normalize(args.get(key)) == str(chat):
        args = dict(args)
        args[key] = chat
        if name == "transcribe_voice":
            args.pop("engine", None)
        return args
    return None


def text_result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


class TelegramScope:
    def __init__(self, upstream, cid, tier, cfg, memory, *, research=False, web_messages=()):
        self.upstream = upstream
        self.tier = tier
        self.chat = int(cid)
        self.research = research
        self.writes_attempted = False
        self.web_blocked = False
        self.web_calls = 0
        self.web_messages = web_messages if not research and tier == "owner" and str(cid) == cfg.get("OWNER_ID") else ()
        self.web_model = web_research.public_model(cfg) if self.web_messages else ""
        self.web_results = {}
        self.self_id = cfg.get("SELF_ID", "")
        target = normalize(cfg.get("NOTIFY_CHAT") or "me")
        own_id = normalize(self.self_id)
        if target in {"me", "self", own_id}:
            self.notify = {"me": "me", "self": "me"}
            if own_id:
                self.notify[own_id] = "me"
        else:
            self.notify = {target: int(target) if target.lstrip("-").isdecimal() else target}
        self.memory = memory
        self.last_result = None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        direct = {"send_message"} if research else (
            OWNER_DIRECT_TOOLS if tier == "owner" else GUEST_TOOLS)
        self.upstream_tools = {item["name"]: item for item in upstream.catalog()
                               if item["name"] not in BLOCKED_TOOLS}
        self.catalog = {name: item for name, item in self.upstream_tools.items() if name in direct}
        if research and "send_message" in self.catalog:
            self.catalog["send_message"] = tool(
                "send_message", "Send one plain text answer to the owner in the requesting chat.",
                {"chat_id": {"type": "integer"}, "message": {"type": "string", "maxLength": 4096}},
            )
        elif not research:
            self.catalog[READ_RESULT["name"]] = READ_RESULT
        if tier == "owner" and not research:
            self.catalog.update({item["name"]: item for item in (MEMORY_READ, MEMORY_WRITE, FIND_TOOL, CALL_TOOL)})
        if self.web_messages:
            self.catalog[WEB_SEARCH["name"]] = WEB_SEARCH

    def no_effects_attempted(self, *, allow_search=False):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            return not self.writes_attempted and (allow_search or not self.web_calls)
        finally:
            self.lock.release()

    def search_web(self, args):
        if self.web_blocked:
            return text_result("Запрос не отправлен: запускай поиск до отправки сообщения или изменения данных.", True)
        raw_query = args.get("query")
        query = web_research.query_from_messages(raw_query, self.web_messages)
        if self.web_model and isinstance(raw_query, str):
            key = web_research.model_query_key(" ".join(raw_query.split()))
            if key in ("@model", web_research.model_query_key(self.web_model)):
                query = self.web_model
        if query is None:
            return text_result("Запрос не отправлен: скопируй короткую публичную фразу целиком из "
                               "новых сообщений владельца без перевода или добавления слов.", True)
        key = query.casefold()
        if key in self.web_results:
            return self.web_results[key]
        if self.web_calls >= 2:
            return text_result("Лимит поиска: два отправленных запроса за запуск.", True)
        self.research = True
        self.last_result = None
        self.web_calls += 1
        results, error = web_research.search(query)
        if error:
            return text_result(error, True)
        result = text_result(json.dumps(results, ensure_ascii=False))
        self.web_results[key] = result
        return result

    def reply_to_research(self, args):
        args = guest_arguments("send_message", args, self.chat, {})
        message = args.get("message") if args is not None else None
        if (self.web_blocked or not isinstance(message, str) or not 0 < len(message) <= 4096
                or args.keys() != {"chat_id", "message"}):
            return text_result("Send one plain text answer in the requesting chat.", True)
        self.writes_attempted = True
        self.web_blocked = True
        return self.upstream.tool_result("send_message", args, timeout=20)

    def page(self, offset):
        result_id, text = self.last_result
        end = offset + TEXT_PAGE
        tail = f"\n\nMore text: read_result(result_id={result_id!r}, offset={end})." if end < len(text) else ""
        return text_result(text[offset:end] + tail)

    def call(self, name, args):
        with self.lock:
            if self.closed.is_set():
                return text_result("This run has ended.", True)
            if name not in self.catalog:
                return text_result("This tool is unavailable in the current run.", True)
            if name == WEB_SEARCH["name"]:
                return self.search_web(args)
            if self.research:
                if name == "send_message":
                    return self.reply_to_research(args)
                return text_result("After web research, only a reply in the requesting chat is available.", True)
            if name == READ_RESULT["name"]:
                offset = args.get("offset")
                if (not self.last_result or args.get("result_id") != self.last_result[0]
                        or type(offset) is not int or not 0 <= offset < len(self.last_result[1])):
                    return text_result("Unknown result or invalid page offset.", True)
                return self.page(offset)
            if name == MEMORY_READ["name"]:
                try:
                    with self.memory.open(encoding="utf-8") as stream:
                        return text_result(stream.read(MAX_MEMORY))
                except FileNotFoundError:
                    return text_result("")
            if name == MEMORY_WRITE["name"]:
                text = args.get("text")
                if not isinstance(text, str) or len(text) > MAX_MEMORY:
                    return text_result("Invalid memory text.", True)
                self.writes_attempted = True
                self.web_blocked = True
                with NamedTemporaryFile(dir=self.memory.parent, mode="w", encoding="utf-8",
                                        prefix=".memory.", delete=False) as stream:
                    staging = Path(stream.name)
                    try:
                        stream.write(text)
                        stream.close()
                        staging.replace(self.memory)
                    finally:
                        staging.unlink(missing_ok=True)
                return text_result("Memory saved.")
            if name == FIND_TOOL["name"]:
                query = args.get("query")
                if not isinstance(query, str) or not query.strip():
                    return text_result("Use part of a Telegram tool name.", True)
                query = query.strip().lower().replace(" ", "_")
                matches = [item for key, item in self.upstream_tools.items() if query in key]
                matches.sort(key=lambda item: (item["name"] != query, item["name"]))
                return self.paginate(text_result(json.dumps(matches[:6], ensure_ascii=False)))
            if name == CALL_TOOL["name"]:
                name, args = args.get("name"), args.get("arguments")
                if not isinstance(name, str) or name not in self.upstream_tools or not isinstance(args, dict):
                    return text_result("Unknown Telegram tool or invalid arguments.", True)
            if self.tier == "guest":
                args = guest_arguments(name, args, self.chat, self.notify)
                if args is None:
                    return text_result("Blocked by guard: this operation is outside the current chat.", True)
                if name == "get_me":
                    # Expose the bot identity without private Telegram account metadata.
                    return text_result(json.dumps({"id": int(self.self_id)}))
            if name not in READ_ONLY_TELEGRAM:
                self.writes_attempted = True
                if name not in CHAT_STATUS_TOOLS:
                    self.web_blocked = True
            return self.paginate(self.upstream.tool_result(name, args, timeout=180))

    def paginate(self, result):
        content = result.get("content", [])
        if (not result.get("isError") and content
                and all(item.get("type") == "text" for item in content)):
            text = "\n".join(item.get("text", "") for item in content)
            if len(text) > TEXT_PAGE:
                self.last_result = (secrets.token_hex(6), text)
                return self.page(0)
        return result

    def rpc(self, request):
        method = request.get("method")
        params = request.get("params") or {}
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {"protocolVersion": requested if requested in VERSIONS else VERSIONS[0],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "telegram-agent", "version": "1.0"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": list(self.catalog.values())}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return text_result("Invalid tool request.", True)
            return self.call(name, arguments)
        raise KeyError(method)


class McpHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Requests include a private URL and may carry Telegram data.

    def reply(self, status, value=None):
        body = json.dumps(value, ensure_ascii=False).encode() if value is not None else b""
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != self.server.authority or origin not in (
                None, f"http://{self.server.authority}"):
            return self.reply(403)
        if self.path != self.server.route:
            return self.reply(404)
        self.connection.settimeout(10)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 1_000_000:
                return self.reply(413)
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                return self.reply(400)
        except (ValueError, TimeoutError):
            return self.reply(400)
        if "id" not in request:
            return self.reply(202)
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = self.server.scope.rpc(request)
        except KeyError:
            response["error"] = {"code": -32601, "message": "Method unavailable"}
        except Exception:
            response["error"] = {"code": -32603, "message": "Telegram tool request failed"}
        try:
            self.reply(200, response)
        except (ConnectionError, TimeoutError):
            pass

    def do_GET(self):
        self.reply(405)

    do_DELETE = do_GET


class Gateway:
    def __init__(self, scope):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), McpHandler)
        self.server.authority = f"127.0.0.1:{self.server.server_port}"
        self.server.route = "/mcp/" + secrets.token_urlsafe(24)
        self.server.scope = scope
        self.url = f"http://{self.server.authority}{self.server.route}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.1,), daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.scope.closed.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
