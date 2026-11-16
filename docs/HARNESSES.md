# Другие агенты вместо Claude Code

Цель: тот же автоответчик на открытом агенте (harness) и открытой модели — DeepSeek, Qwen и т. п.,
на домашнем сервере.

**Статус:** сейчас работает только Claude Code. Переход отложен до появления ключа API
открытой модели (DeepSeek). Ниже — что для этого нужно и что умеют кандидаты.
Сведения взяты из документации проектов на 2026-11-16, вживую ничего из этого **не проверялось**.

## Что автоответчик требует от агента

| Возможность | Зачем | В Claude Code |
|---|---|---|
| Один запуск без интерфейса: задача → работа → выход | запуск на каждое сообщение | `claude -p … --output-format json` |
| Свой системный промпт на запуск | персона, режим, память | `--system-prompt` |
| MCP по streamable HTTP, только этот сервер | инструменты Telegram | `--strict-mcp-config --mcp-config` |
| Белый список инструментов на запуск | у гостя 17 инструментов, у владельца все | `--allowedTools`, `--tools` |
| Хук до вызова, который блокирует по аргументам | guest — только текущий чат | PreToolUse, exit 2 (`guard.py`) |
| Хук после вызова, который подменяет результат | большие чтения (`reflow.py`) | PostToolUse `updatedMCPToolOutput` |
| Лимит шагов и таймаут | цена и зависания | `--max-turns`, таймаут в диспетчере |
| Без сохранения сессий и чужой памяти | изоляция | `--no-session-persistence`, auto-memory off |

Самое важное здесь — **guard**: без него гость может уговорить бота читать чужие чаты.

## Кандидаты

| | Qwen Code | Hermes Agent (Nous Research) | OpenCode | DeepSeek |
|---|---|---|---|---|
| Лицензия | Apache-2.0 | MIT | MIT | официальный harness `dsh` (MIT) — v0.1 developer preview (2026-08); официальность репозитория не подтверждена |
| Запуск без интерфейса | `qwen -p`, `--output-format json` | `hermes chat -q "…"` / `--query-file` | `opencode run "…"` | не найден |
| Лимит шагов | `--max-session-turns`, `--max-tool-calls`, `--max-wall-time` | `--max-turns` | не подтверждено | — |
| Системный промпт на запуск | `--system-prompt` (QWEN.md всё равно добавляется) | через SOUL.md/AGENTS.md; флаг не подтверждён | не подтверждено | — |
| MCP по HTTP | да (`httpUrl`), `includeTools`/`excludeTools` на сервер | да (`url`), `tools.include/exclude` | да (`type: remote`) | — |
| Белый список на запуск | для MCP — `includeTools` в конфиге; аналога `--allowedTools` нет | `--toolsets` + include/exclude | правила permissions, `deny` действует и без интерфейса | — |
| Хук «блокировать по аргументам» | да: PreToolUse, `permissionDecision: deny` | да: плагин `pre_tool_call` → `{"action": "block"}`; ошибка плагина = блок | плагины `tool.execute.before` (семантика не подтверждена) | — |
| Хук «подменить результат» | не документирован | да: `transform_tool_result` | не подтверждено | — |
| DeepSeek / Qwen API | да: `modelProviders.openai`, `baseUrl: https://api.deepseek.com/v1`; DashScope для Qwen | свой OpenAI-совместимый endpoint (детали не подтверждены) | вероятно (OpenAI-совместимый), не проверено | родной |
| Своё в комплекте | — | память, навыки, шлюз Telegram **через Bot API** (токен BotFather), то есть не для пользовательского аккаунта | — | — |

Кроме того, с моделями DeepSeek совместимы сторонние DeepSeek-TUI и Deep Code CLI.

Источники:
- [Qwen Code](https://github.com/QwenLM/qwen-code), его документация:
  [headless](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/),
  [hooks](https://qwenlm.github.io/qwen-code-docs/en/users/features/hooks/),
  [MCP](https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/),
  [провайдеры](https://qwenlm.github.io/qwen-code-docs/en/users/configuration/model-providers/);
- [Hermes Agent](https://github.com/NousResearch/hermes-agent), документация в
  `website/docs/user-guide/` репозитория: cli, features/mcp, features/hooks, messaging/telegram;
- [OpenCode](https://github.com/anomalyco/opencode), его
  [MCP](https://opencode.ai/docs/mcp-servers/) и [permissions](https://opencode.ai/docs/permissions/);
- [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness).

## Вывод

Ни у одного кандидата нет всего набора Claude Code сразу, а хуки у всех устроены по-разному.
Переписывать guard под каждый агент — путь к ошибкам безопасности. Поэтому план — **вынести всё
важное из агента в MCP-прокси**.

## План v2: шлюз между агентом и telegram-mcp

```
агент (любой) ──HTTP──► tg-gate (127.0.0.1:8766/mcp/<токен запуска>) ──HTTP──► telegram-mcp (:8765)
```

`tg-gate` — небольшой MCP-сервер на Python (библиотека `mcp` уже есть в venv telegram-mcp).

**Токен запуска.** Диспетчер перед каждым запуском создаёт токен с политикой: режим, текущий чат,
`NOTIFY_CHAT`, список инструментов. Агенту дают только URL с этим токеном.

**Что делает шлюз:**

- **Список инструментов.** `tools/list` отдаёт только разрешённые для этого режима: белый список
  на стороне сервера, флаги агента не нужны.
- **guard.** `tools/call` проверяет аргументы по тем же правилам, что `guard.py`, и при нарушении
  возвращает ошибку «Blocked by guard». Работает одинаково для любого агента.
- **Большие результаты.** Шлюз сам режет их на страницы и даёт инструмент
  `read_result(id, offset)`. `Read`, файлы и `reflow.py` не нужны.
- **Память.** Инструменты `memory_read` и `memory_add`/`memory_remove`, только для owner. Агенту
  не нужен доступ к файлам вообще.

**Что остаётся у агента:**

- запуск без интерфейса;
- системный промпт;
- один MCP-сервер по HTTP;
- выключенные встроенные инструменты (файлы, shell): `--exclude-tools` у Qwen Code, toolsets у
  Hermes, permissions `deny` у OpenCode.

**Адаптеры.** В `config.env` появится `HARNESS=claude|qwen|hermes|opencode` и шаблон команды для
каждого агента. Хуки агента, где они есть, остаются второй линией защиты.

**Порядок работ:**

1. tg-gate и его тесты.
2. Перевести на него Claude Code, проверить на живом боте: всё прежнее должно работать.
3. Адаптер Qwen Code + DeepSeek API: ближе всего к Claude Code, конфигурация DeepSeek
   документирована.
4. Адаптер Hermes, затем OpenCode.
5. `dsh` от DeepSeek — когда у него появятся запуск без интерфейса и MCP.

Для шагов 3–5 нужен ключ API выбранной модели (DeepSeek, DashScope).

### Промежуточный вариант

DeepSeek даёт Anthropic-совместимый API, и Claude Code умеет с ним работать
([инструкция DeepSeek](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code)).
Достаточно вписать `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` и модели в `backend.env`, а в
`config.env` поставить `MODEL=deepseek-flash`.

Агент при этом остаётся Claude Code, со всеми его хуками. Не проверено.
