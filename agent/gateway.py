"""Per-run Streamable HTTP MCP gateway. Telegram access is checked before forwarding."""

import json
import secrets
import threading
from tempfile import NamedTemporaryFile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import followups
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
WEB_CONTEXT_READS = {"get_history", "get_messages_after", "get_message_context",
                     "get_messages", "list_messages", "search_messages"}
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
FOLLOWUP = tool("owner_followup", "Read, remember, or clear the owner's pending request to send a "
                "message in another chat. Remember before asking for clarification. Read when the latest message "
                "continues an earlier task. Clear when the owner cancels or the task is finished.",
                {"action": {"type": "string", "enum": ["read", "remember", "clear"]}})

FIND_TOOL = tool("find_telegram_tool", "Find additional Telegram tools and their exact input schemas.",
                 {"query": {"type": "string", "description": "Part of a tool name, such as 'folder'."}})
CALL_TOOL = tool("call_telegram_tool", "Call an additional Telegram tool using its exact input schema.",
                 {"name": {"type": "string"}, "arguments": {"type": "object"}})
WEB_SEARCH = tool("search_web", "Search the public web for current facts or sources. Use a short "
                  "query about public information, using conversation context when needed. "
                  "For the configured public "
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
    def __init__(self, upstream, cid, tier, cfg, memory, *, research=False, followup_ids=()):
        self.upstream = upstream
        self.tier = tier
        self.chat = int(cid)
        self.research = research
        self.writes_attempted = False
        self.web_blocked = False
        self.web_calls = 0
        self.allow_web = (not research and tier == "owner" and str(cid) == cfg.get("OWNER_ID")
                          and cfg.get("WEB_SEARCH") == "duckduckgo")
        self.web_model = web_research.public_model(cfg) if self.allow_web else ""
        self.web_fallback = cfg.get("WEB_SEARCH_FALLBACK") == "firecrawl"
        self.web_identifiers = (cfg.get("OWNER_USERNAME", "").strip().lstrip("@"),
                                cfg.get("SELF_ID", ""), cfg.get("OWNER_ID", ""))
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
        self.followup_ids = followups.message_ids(followup_ids)
        self.followup_store = None
        self.followup_loaded = False
        self.followup_restored = False
        self.followup_delivery_attempted = False
        self.followup_waiting_for_question = False
        if (self.followup_ids and not research and tier == "owner"
                and str(cid) == cfg.get("OWNER_ID") and self.self_id):
            self.followup_store = followups.Followups(
                memory.parent / "state" / "owner-followup.json", cid, self.self_id)
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
        if self.allow_web:
            self.catalog[WEB_SEARCH["name"]] = WEB_SEARCH
        if self.followup_store is not None:
            self.catalog[FOLLOWUP["name"]] = FOLLOWUP

    def owner_followup(self, action):
        if action not in ("read", "remember", "clear"):
            return text_result("Choose read, remember, or clear.", True)
        try:
            record = self.followup_store.read()
            if action == "read":
                if record is None:
                    return text_result("Нет сохранённого поручения.")
                self.web_blocked = True
                self.followup_loaded = True
                try:
                    messages = followups.restore(self.upstream, self.chat, self.self_id, record)
                except (OSError, RuntimeError, ValueError, AttributeError, KeyError):
                    if record["status"] == "pending":
                        raise
                    messages = []
                self.followup_restored = True
                fields = {"status": record["status"], "messages": messages}
                if "delivered_id" in record:
                    fields["delivered_id"] = record["delivered_id"]
                return text_result(json.dumps(fields, ensure_ascii=False))
            self.web_blocked = True
            if action == "clear":
                if self.followup_delivery_attempted:
                    return text_result("Попытка отправки сохранена для следующего запуска.")
                self.writes_attempted = True
                self.followup_store.clear()
                self.followup_loaded = self.followup_restored = False
                self.followup_waiting_for_question = False
                return text_result("Сохранённое поручение закрыто.")
            if record and (record["status"] == "delivery_unknown"
                           or (record["status"] == "pending" and not self.followup_restored)):
                return text_result("Сначала проверь открытое поручение.", True)
            prior_ids = record["message_ids"] if record and record["status"] == "pending" else []
            self.writes_attempted = True
            self.followup_store.write((*prior_ids, *self.followup_ids),
                                      question_id=record.get("question_id") if prior_ids else None)
            self.followup_waiting_for_question = True
            return text_result("Ссылки на поручение сохранены.")
        except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
            return text_result("Не удалось восстановить или обновить поручение. Уточни у владельца.", True)

    def deliver_followup(self, name, args):
        if name != "send_message":
            return text_result("Для выполнения сохранённого поручения используй send_message.", True)
        try:
            record = self.followup_store.read()
            if (not self.followup_restored or not record or record["status"] != "pending"
                    or self.followup_delivery_attempted):
                return text_result("Повторная отправка по этому поручению остановлена. Проверь доставку.", True)
            self.followup_store.write(record["message_ids"], "delivery_unknown",
                                      question_id=record.get("question_id"))
        except (OSError, ValueError):
            return text_result("Не удалось зафиксировать попытку отправки.", True)
        self.followup_delivery_attempted = True
        result = self.upstream.tool_result(name, args, timeout=180)
        receipt = followups.delivery_id(result)
        if receipt:
            try:
                self.followup_store.write(record["message_ids"], "sent", delivered_id=receipt,
                                          question_id=record.get("question_id"))
            except OSError:
                # Leave the durable attempt in place even if saving the receipt fails.
                pass
        return self.paginate(result)

    def remember_question(self, result):
        mid = followups.delivery_id(result)
        if not mid:
            return
        self.followup_waiting_for_question = False
        try:
            record = self.followup_store.read()
            if record and record["status"] == "pending":
                self.followup_store.write(record["message_ids"], question_id=mid)
        except (OSError, ValueError):
            pass  # The owner request is still recorded even if the question ID cannot be saved.

    def no_effects_attempted(self, *, allow_search=False):
        if not self.lock.acquire(blocking=False):
            return False
        try:
            return not self.writes_attempted and (allow_search or not self.web_calls)
        finally:
            self.lock.release()

    def search_web(self, args):
        if self.web_blocked:
            return text_result("Запрос не отправлен: используй поиск до отправки сообщения, изменения "
                               "данных или чтения приватного контекста вне текущей лички.", True)
        query = web_research.normalize_query(args.get("query"))
        if not query:
            return text_result(f"Укажи поисковый запрос длиной до {web_research.MAX_QUERY} символов.", True)
        if query == "@model":
            if not self.web_model:
                return text_result("Публичное имя текущей модели не настроено.", True)
            query = self.web_model
        elif self.web_model and web_research.model_query_key(query) == web_research.model_query_key(self.web_model):
            query = self.web_model
        if web_research.contains_private_identifier(query, self.web_identifiers):
            return text_result("Запрос содержит идентификатор или приватные данные. "
                               "Переформулируй запрос без них.", True)
        key = query.casefold()
        if key in self.web_results:
            return self.web_results[key]
        if self.web_calls >= 2:
            return text_result("Лимит поиска: два отправленных запроса за запуск.", True)
        self.research = True
        self.last_result = None
        self.web_calls += 1
        results, error = web_research.search(query, fallback=self.web_fallback)
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
            if name == FOLLOWUP["name"]:
                return self.owner_followup(args.get("action"))
            if name == READ_RESULT["name"]:
                offset = args.get("offset")
                if (not self.last_result or args.get("result_id") != self.last_result[0]
                        or type(offset) is not int or not 0 <= offset < len(self.last_result[1])):
                    return text_result("Unknown result or invalid page offset.", True)
                return self.page(offset)
            if name == MEMORY_READ["name"]:
                self.web_blocked = True
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
            elif (self.allow_web and (name not in WEB_CONTEXT_READS
                  or normalize(args.get("chat_id")) != str(self.chat) or args.get("account") is not None)):
                self.web_blocked = True
            owner_message = (name == "send_message" and normalize(args.get("chat_id")) == str(self.chat)
                             and args.get("account") is None)
            if (self.followup_loaded and not owner_message and name not in READ_ONLY_TELEGRAM
                    and name not in CHAT_STATUS_TOOLS):
                if args.get("account") is None:
                    return self.deliver_followup(name, args)
                return text_result("Сохранённое поручение привязано к текущему Telegram-аккаунту.", True)
            result = self.upstream.tool_result(name, args, timeout=180)
            if self.followup_waiting_for_question and owner_message:
                self.remember_question(result)
            return self.paginate(result)

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
