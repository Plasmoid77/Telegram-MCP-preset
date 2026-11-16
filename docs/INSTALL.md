# Установка с нуля

Полная установка на Linux с systemd: ноутбук, ПК или домашний сервер. Пути даны для текущего
пользователя (`~`), root не нужен; исключение — `loginctl enable-linger` на сервере.

## 0. Что понадобится

| Что | Зачем |
|---|---|
| Отдельный Telegram-аккаунт | им будет управлять ИИ. Основной аккаунт не используйте |
| Ваш основной аккаунт | владелец: с него вы даёте боту поручения |
| `api_id` / `api_hash` | приложение на [my.telegram.org](https://my.telegram.org/apps): Platform — Desktop, short name без дефиса. Можно создать на любом своём аккаунте |
| Linux с systemd, Python ≥ 3.10, `git`, [`uv`](https://docs.astral.sh/uv/getting-started/installation/), `ffmpeg` | сервер, диспетчер, расшифровка голосовых |
| ~1 ГБ на диске сверх telegram-mcp | `asr-server`: окружение 160 МБ + модель GigaAM-v3 int8 225 МБ (скачивается при первом голосовом) |
| [Claude Code](https://code.claude.com/docs/en/setup) с подпиской Claude или ключом API | агент, который отвечает (другие агенты — [HARNESSES.md](HARNESSES.md)) |
| Телефон с Telegram аккаунта-бота | войти по QR-коду |

Правило для всей установки: **секреты (`api_hash`, строка сессии, файл `*.session`, ключи API)
вводит человек в своём терминале.** Их не вставляют в чат с ИИ, не коммитят и не пересылают.

## 1. Скачать пресет и запустить установщик

```bash
git clone https://github.com/Plasmoid77/Telegram-MCP-preset ~/Telegram-MCP-preset
cd ~/Telegram-MCP-preset
./install.sh
```

`install.sh` делает следующее:

1. Клонирует `https://github.com/chigwell/telegram-mcp` в `~/.local/share/telegram-mcp`, ставит
   тег `v3.3.22` (другой: `TELEGRAM_MCP_REF=vX.Y.Z ./install.sh`) и выполняет `uv sync`.
   **Только из git:** пакет `telegram-mcp` на PyPI — чужой проект с тем же именем.
2. Создаёт `~/.local/share/telegram-mcp/.env` (600) из `server/env.example`.
3. Создаёт папку `~/TelegramMCP` (700). Это единственная папка, с которой работают файловые
   инструменты MCP.
4. Ставит автоответчик в `~/.local/share/telegram-agent/`:
   - код: `dispatcher.py`, `guard.py`, `reflow.py`, `settings.json`, `mcp.json`;
   - если их ещё нет — `prompt.md`, `config.env` (600) и `backend.env` (600).
5. Ставит `asr-server` (локальная расшифровка голосовых, onnx-asr) в
   `~/.local/share/asr-server/` по зафиксированному `asr/uv.lock`.
6. Кладёт юниты в `~/.config/systemd/user/` и выполняет `daemon-reload`.

Сервисы установщик не запускает и не перезапускает. Повторный запуск безопасен: код обновляется,
прежняя версия файла сохраняется в `telegram-agent/state/*.bak-<время>`. Ваши `.env`,
`config.env`, `prompt.md`, `memory.md` и `backend.env` не трогаются.

## 2. Секреты сервера

Откройте `~/.local/share/telegram-mcp/.env` в редакторе и впишите:

```
TELEGRAM_API_ID=<число>
TELEGRAM_API_HASH=<строка>
```

## 3. Проверить, что второй копии сессии нигде нет

Если аккаунт уже подключён к telegram-mcp на другой машине, сначала остановите его там:

```bash
systemctl --user disable --now telegram-agent telegram-mcp
```

Одна и та же сессия на двух машинах даёт `AuthKeyDuplicatedError`, и Telegram может её
аннулировать.

## 4. Войти в Telegram (в своём терминале, не через ИИ)

Это шаги из upstream README: «Generate a Session String» и «Persistent Session».

```bash
cd ~/.local/share/telegram-mcp
uv run session_string_generator.py --qr      # QR: Telegram на телефоне аккаунта-бота →
                                             # Настройки → Устройства → Подключить устройство
```

1. На вопрос об обновлении `.env` ответьте `y`: строка сессии запишется в `.env`.
2. Переведите её в файловую сессию. Она надёжнее для долгой работы, потому что хранит состояние
   обновлений:
   ```bash
   uv run telegram-mcp-migrate-session
   ```
3. Сделайте в `.env` то, что скажет команда: закомментируйте `TELEGRAM_SESSION_STRING` и
   пропишите `TELEGRAM_SESSION_NAME=/home/<user>/.local/share/telegram-mcp/telegram_mcp_session`.

Файл `telegram_mcp_session.session` — это **полный доступ к аккаунту**. Сделайте ему права 600 и
бэкап в зашифрованном виде.

Если вход по QR невозможен, есть `--phone` (код придёт в Telegram).

## 5. Запустить сервер

```bash
systemctl --user enable --now asr-server     # расшифровка голосовых, нужна серверу ниже
systemctl --user enable --now telegram-mcp
journalctl --user -u telegram-mcp -n 20 --no-pager
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/mcp   # любой код кроме 000 = слушает
```

Настройки сервера лежат в юните. Что выбрано и почему — в [PRESET.md](PRESET.md#1-настройки-сервера):

```
MCP_TRANSPORT=http  MCP_HOST=127.0.0.1  MCP_PORT=8765
TELEGRAM_EXPOSED_TOOLS=all
TELEGRAM_ALLOWED_ROOTS=%h/TelegramMCP  TELEGRAM_SERVER_ROOTS_ONLY=1
TELEGRAM_EVENT_FEED=1
TELEGRAM_TRANSCRIBE=auto  TELEGRAM_TRANSCRIBE_ENGINE=openai
TELEGRAM_TRANSCRIBE_OPENAI_URL=http://127.0.0.1:5093/v1  TELEGRAM_TRANSCRIBE_LANGUAGE=ru
```

## 6. Настроить автоответчик

Файл `~/.local/share/telegram-agent/config.env`: замените все `<…>`.

| Ключ | Что вписать |
|---|---|
| `OWNER_ID` | числовой id вашего **основного** аккаунта. Узнать: напишите что-нибудь аккаунту-боту и выполните `get_me` со своего аккаунта, либо спросите [@userinfobot](https://t.me/userinfobot), либо подключите MCP к Claude Code (шаг 8) и попросите «resolve_username @вашник» |
| `OWNER_NAME`, `OWNER_USERNAME` | ваше имя и `@ник`: бот так вас узнаёт и обращается |
| `SELF_ID` | id аккаунта-бота (`get_me` через MCP) |
| `ACCOUNT_NAME` | как бот называет свой аккаунт (имя или `@ник`) |
| `NOTIFY_CHAT` | куда слать уведомления секретаря: обычно = `OWNER_ID` |
| `BOT_NAME` | имя персоны (по умолчанию «Нейросетевой Doom bot») |
| `GROUP_TRIGGERS` | слова-обращения в группах, через запятую. Впишите и имя аккаунта |
| `MODEL` | модель агента |

Остальные ключи описаны в [USAGE.md](USAGE.md#configenv). Файл перечитывается на лету.

Характер бота задаёт `prompt.md`. `{{KEY}}` в нём подставляются из `config.env`.

## 7. Агент: Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash   # официальный установщик → ~/.local/bin/claude
claude                                           # один раз: /login
```

- **Сервер без браузера.** На машине с браузером выполните `claude setup-token` и впишите токен в
  `~/.local/share/telegram-agent/backend.env` как `CLAUDE_CODE_OAUTH_TOKEN=…` (файл 600).
  Вместо подписки можно указать `ANTHROPIC_API_KEY=…`.
- **Другой путь к `claude`.** Если он стоит не в `~/.local/bin` и не в `PATH` сервиса, укажите
  `CLAUDE_BIN=` в `config.env`.

Для запусков бота auto-memory Claude Code выключена принудительно, и это нельзя убирать. Если
домашняя папка — git-репозиторий, иначе бот подхватил бы ваш личный `MEMORY.md`. Подробнее — в
[PRESET.md](PRESET.md#изоляция).

## 8. (По желанию) Подключить MCP к своим интерактивным агентам

Все клиенты ходят в **один** сервер по HTTP. Запускать второй экземпляр (stdio, Docker) нельзя:
файловая сессия блокируется на один процесс.

```bash
claude mcp add --scope user --transport http telegram http://127.0.0.1:8765/mcp
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.telegram]
url = "http://127.0.0.1:8765/mcp"
```

Любой другой клиент: streamable HTTP, `http://127.0.0.1:8765/mcp`.

## 9. Запустить автоответчик

```bash
systemctl --user enable --now telegram-agent
journalctl --user -u telegram-agent -f
```

## 10. Работа без входа в систему (сервер, перезагрузки)

Пользовательские сервисы стартуют при входе пользователя и останавливаются при выходе. На сервере
нужно, чтобы они работали всегда:

```bash
sudo loginctl enable-linger "$USER"
```

Ещё для сервера:

- **Часовой пояс.** Бот получает «текущее время» по часовому поясу машины. Установите его:
  `timedatectl set-timezone Europe/Moscow`.
- **Сон и выключение.** Пока машина спит или выключена, бот не отвечает. Пропущенные за это время
  личные сообщения потом не обрабатываются.

## 11. Проверка

1. Напишите боту в личку со своего аккаунта: «привет, что умеешь?». В журнале появится
   `tier=owner rc=0 error=False`, и придёт ответ.
2. Попросите кого-нибудь написать боту. В журнале будет `tier=guest`.
3. Добавьте бота в группу и напишите ему в личку: «разрешаю тебе писать в группе <название>».
   - Бот ответит итоговым списком групп.
   - В журнале появится `groups folder '…': [id]`.
   - В группе напишите «<слово из GROUP_TRIGGERS>, проверка». Ответ придёт reply через ~10 с.
4. «Запомни: …» — бот скажет, что записал. Проверка: `cat ~/.local/share/telegram-agent/memory.md`.
5. Голосовое боту длиннее минуты. Ответ по смыслу, в журнале `asr-server` будет строка
   `NN.Ns audio -> NNN chars`.
6. Большая задача, например «сколько сообщений в топике <тема> группы <группа>?». Должна пройти
   без жалоб на размер.

## Перенос на другую машину

1. На старой машине: `systemctl --user disable --now telegram-agent telegram-mcp`.
2. На новой: шаг 1 (`install.sh`).
3. Перенесите на новую машину безопасным способом (не через чат с ИИ):
   - `~/.local/share/telegram-mcp/.env` и `telegram_mcp_session.session`;
   - `~/.local/share/telegram-agent/config.env`, `memory.md`, `prompt.md` (если меняли) и
     `backend.env`.
4. Если сменились пользователь или путь, поправьте `TELEGRAM_SESSION_NAME` в `.env`. Остальные
   пути вычисляются сами.
5. Шаги 5, 7, 9, 10, 11. Модель распознавания скачается заново при первом голосовом.

Можно и без переноса сессии: заново войти по шагу 4. Старую сессию затем завершите в Telegram:
«Устройства».

## Удаление

```bash
systemctl --user disable --now telegram-agent telegram-mcp asr-server
rm ~/.config/systemd/user/{telegram-agent,telegram-mcp,asr-server}.service && systemctl --user daemon-reload
rm -r ~/.local/share/telegram-agent ~/.local/share/telegram-mcp   # вместе с сессией!
rm -r ~/.local/share/asr-server ~/.cache/huggingface/hub/models--istupakov--*
```

После этого завершите сессию в Telegram: «Настройки → Устройства».
