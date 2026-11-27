# Установка с нуля

Полная установка на Linux с systemd: ноутбук, ПК или домашний сервер. Пути даны для текущего
пользователя (`~`), установщик работает без root. Права администратора нужны для системных пакетов и `linger` на сервере.

В v2 код пресета живёт в git-чекауте (по умолчанию `~/.local/share/telegram-agent`), настройки —
в `~/.config/telegram-agent/`, состояние и память — в `~/.local/state/telegram-agent/`. Подробнее
об устройстве — в [DESIGN.md](DESIGN.md).

## 0. Что понадобится

| Что | Зачем |
|---|---|
| Отдельный Telegram-аккаунт | им будет управлять ИИ. Основной аккаунт не используйте |
| Ваш основной аккаунт | владелец: с него вы даёте боту поручения |
| `api_id` / `api_hash` | приложение на [my.telegram.org](https://my.telegram.org/apps): Platform — Desktop, short name без дефиса. Можно создать на любом своём аккаунте |
| Linux с systemd, Python ≥ 3.10, `git`, [`uv`](https://docs.astral.sh/uv/getting-started/installation/), `ffmpeg` | сервер, диспетчер, расшифровка голосовых |
| ~1.5 ГБ на диске сверх telegram-mcp | `asr-server`: окружение 160 МБ + модель GigaAM-v3 885 МБ (скачивается при первом голосовом; `int8` — 225 МБ) |
| Claude Code, Codex или OpenCode с рабочей авторизацией | агент, который отвечает. См. [DESIGN.md](DESIGN.md#агенты-harness) |
| Телефон с Telegram аккаунта-бота | войти по QR-коду |

Правило для всей установки: **секреты (`api_hash`, строка сессии, файл `*.session`, ключи API)
вводит человек в своём терминале.** Их не вставляют в чат с ИИ, не коммитят и не пересылают.

## 1. Скачать пресет и запустить установщик

Клонируйте пресет сразу в каталог, где он останется жить: юниты указывают на этот чекаут,
поэтому обновление пресета — это `git checkout` нового тега плюс перезапуск.

На Debian, Ubuntu или LMDE системные зависимости можно поставить командой
`sudo apt install git python3 ffmpeg`. Установите [uv](https://docs.astral.sh/uv/getting-started/installation/)
и проверьте, что `uv --version` работает из вашего терминала.

```bash
git clone https://github.com/Plasmoid77/Telegram-MCP-preset ~/.local/share/telegram-agent
cd ~/.local/share/telegram-agent
./install.sh
```

`install.sh` делает следующее:

1. Клонирует `https://github.com/chigwell/telegram-mcp` в `~/.local/share/telegram-mcp`, ставит
   тег `v3.3.35` (другой: `TELEGRAM_MCP_REF=vX.Y.Z ./install.sh`) и выполняет `uv sync`.
   **Только из git:** пакет `telegram-mcp` на PyPI — чужой проект с тем же именем.
2. Создаёт `~/.local/share/telegram-mcp/.env` (600) из `server/env.example`.
3. Создаёт `~/.config/telegram-agent/` (700) с `config.env` и `backend.env` (600) из шаблонов,
   если их ещё нет, и `~/.local/state/telegram-agent/` (700).
4. Создаёт папку `~/TelegramMCP` (700). Это единственная папка, с которой работают файловые
   инструменты MCP.
5. Готовит окружение `asr-server` (`asr/uv.lock`, `uv sync --frozen`), каталог
   `~/.cache/huggingface` и проверяет `ffmpeg`.
6. Кладёт юниты в `~/.config/systemd/user/`, подставив путь чекаута, и выполняет `daemon-reload`.

Сервисы установщик не запускает и не перезапускает. Повторный запуск безопасен: шаблоны
`config.env` и `backend.env` создаются один раз и не перезаписываются, секреты не трогаются.

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
curl -fsS http://127.0.0.1:5093/health
systemctl --user is-active asr-server telegram-mcp
```

Подключите клиент по шагу 8 и вызовите `get_me`, чтобы подтвердить вход в нужный аккаунт.

Настройки сервера лежат в юните. Что выбрано и почему — в [DESIGN.md](DESIGN.md):

```
MCP_TRANSPORT=http  MCP_HOST=127.0.0.1  MCP_PORT=8765
TELEGRAM_EXPOSED_TOOLS=all
TELEGRAM_ALLOWED_ROOTS=%h/TelegramMCP  TELEGRAM_SERVER_ROOTS_ONLY=1
TELEGRAM_CATCH_UP_INTERVAL=120
TELEGRAM_TRANSCRIBE=auto  TELEGRAM_TRANSCRIBE_ENGINE=openai
TELEGRAM_TRANSCRIBE_OPENAI_URL=http://127.0.0.1:5093/v1  TELEGRAM_TRANSCRIBE_LANGUAGE=ru
```

## 6. Настроить автоответчик

Файл `~/.config/telegram-agent/config.env`: замените все `<…>`.

| Ключ | Что вписать |
|---|---|
| `OWNER_ID` | числовой id вашего **основного** аккаунта. Узнать: напишите что-нибудь аккаунту-боту и выполните `get_me` со своего аккаунта, либо спросите [@userinfobot](https://t.me/userinfobot), либо подключите MCP к Claude Code (шаг 8) и попросите «resolve_username @вашник» |
| `OWNER_NAME`, `OWNER_USERNAME` | ваше имя и `@ник`: бот так вас узнаёт и обращается |
| `SELF_ID` | id аккаунта-бота (`get_me` через MCP) |
| `ACCOUNT_NAME` | как бот называет свой аккаунт (имя или `@ник`) |
| `NOTIFY_CHAT` | куда слать уведомления секретаря: обычно = `OWNER_ID` |
| `BOT_NAME` | имя персоны (по умолчанию «Нейросетевой Doom bot») |
| `GROUP_TRIGGERS` | слова-обращения в группах, через запятую. Впишите и имя аккаунта |
| `HARNESS`, `MODEL` | бэкенд и модель по шагу 7 |

Остальные ключи описаны в [OPERATIONS.md](OPERATIONS.md#configenv). Файл перечитывается на лету.

Характер и правила бота задают промпты в `agent/prompts/` (`persona.md`, `base.md`, `owner.md`,
`guest.md`, `group.md`); `{{KEY}}` в них подставляются из `config.env`. Чтобы заменить персону,
положите рядом с `config.env` свой `persona.md` — он перекроет шаблон.

## 7. Установить и авторизовать агент

Команды входа выполняйте под тем же пользователем, что запускает службы.

| Бэкенд | Как подготовить | Что указать в `config.env` |
|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/setup) | Установить и выполнить `claude auth login`. Для headless-сервера можно использовать `claude setup-token` или ключ Anthropic в `backend.env` | `HARNESS=claude`, `MODEL=claude-sonnet-5-5` |
| [Codex](https://learn.chatgpt.com/docs/non-interactive-mode) | Установить CLI и выполнить `codex login` или `codex login --device-auth`. Проверить `codex login status` | `HARNESS=codex`, `MODEL=gpt-6.2-sol` |
| [OpenCode](https://opencode.ai/docs/) | Установить CLI v2.3.2 или новее. В `opencode` выполнить `/connect`, выбрать провайдера, проверить модели через `/models` | `HARNESS=opencode`, например `MODEL=opencode-go/qwen3.7-plus` |

Идентификаторы моделей в таблице — примеры. Выберите доступную модель своего провайдера.
Для `HARNESS=claude` `MODEL` можно оставить пустым (берётся модель Claude Code); для остальных
бэкендов он обязателен. В [DESIGN.md](DESIGN.md#агенты-harness) описаны дополнительные настройки,
изоляция и формат внешнего раннера (`HARNESS=command`).

### Сменить модель автоответчика OpenCode

На компьютере, где работает `telegram-agent`, откройте `~/.config/telegram-agent/config.env`.
При `HARNESS=opencode` задайте в `MODEL` полный идентификатор модели, например
`MODEL=opencode-go/qwen3.7-plus`. Список моделей Go можно посмотреть в терминале.

```bash
opencode models | grep '^opencode-go/'
```

Если `opencode` установлен вне `PATH`, используйте полный путь через `OPENCODE_BIN`, например
`OPENCODE_BIN=~/.opencode/bin/opencode`. После сохранения файла новая модель применяется со
следующего запуска автоответчика. Перезапуск службы не нужен.

Команда `/models` выбирает модель интерактивной сессии OpenCode. Автоответчик при каждом запуске
передаёт собственное значение `MODEL` из `config.env`.

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

OpenCode v2, в каталоге своего проекта:

```bash
opencode mcp add telegram --url http://127.0.0.1:8765/mcp
opencode mcp list
```

Клиент владельца получает доступ ко всему аккаунту. Оставьте адрес на loopback. Автоответчик
подключается отдельно, через свой per-run брокер, согласно выбранному `HARNESS`.

## 9. Запустить автоответчик

```bash
python3 -I ~/.local/share/telegram-agent/agent/dispatcher.py --check   # должно вывести CHECK OK
systemctl --user enable --now telegram-agent
journalctl --user -u telegram-agent -f
```

Проверка новых релизов telegram-mcp раз в неделю. Она только сообщает (журнал и сообщение в
`NOTIFY_CHAT`), а обновление запускается вручную ([OPERATIONS.md](OPERATIONS.md), «Обслуживание»):

```bash
systemctl --user enable --now telegram-mcp-update.timer
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
  обращения обрабатываются после запуска, пока их возраст укладывается в `MAX_AGE` и они доступны в истории.

## 11. Проверка

1. Напишите боту в личку со своего аккаунта: «привет, что умеешь?». В журнале появится
   `chat=<id> owner harness=… ok=True rc=0 …`, и придёт ответ.
2. Попросите кого-нибудь написать боту. В журнале будет `chat=<id> guest …`.
3. Добавьте бота в группу и напишите ему в личку: «разрешаю тебе писать в группе <название>».
   - Бот ответит итоговым списком групп.
   - В журнале появится `groups folder '…': [id]`.
   - В группе напишите «<слово из GROUP_TRIGGERS>, проверка». Ответ придёт reply через ~10 с.
4. «Запомни: …» — бот скажет, что записал. Проверка: `cat ~/.local/state/telegram-agent/memory.md`.
5. Голосовое боту длиннее минуты. Ответ по смыслу, в журнале `asr-server` будет строка
   `NN.Ns audio -> NNN chars`.
6. Большая задача, например «сколько сообщений в топике <тема> группы <группа>?». Должна пройти
   без жалоб на размер (длинные результаты читаются по страницам через `read_result`).

## Переход с v1

Скрипт `scripts/migrate-v1.sh` переносит установку v1 (файлы, скопированные в
`~/.local/share/telegram-agent`) на раскладку v2. Бот не работает только между шагами 4 и 8,
около минуты; ничего не удаляется.

```bash
git clone -b <тег> https://github.com/Plasmoid77/Telegram-MCP-preset ~/.local/share/telegram-agent.new
~/.local/share/telegram-agent.new/scripts/migrate-v1.sh
```

Что делает скрипт по шагам:

1. проверяет ваш `config.env` новой версией (`dispatcher.py --check`: агент, модель, вход в
   telegram-mcp); при ошибке ничего не меняет;
2. готовит окружение `asr-server` в новом чекауте, пока бот ещё работает;
3. пишет `rollback.sh` и останавливает еженедельную проверку релизов (во время
   `update_mcp.py apply` скрипт не запускается);
4. ставит бота на паузу (ждёт конца текущего ответа) и останавливает его; если пауза не
   подтвердилась за 15 мин — возвращает всё как было;
5. копирует настройки в `~/.config/telegram-agent/`, состояние и память — в
   `~/.local/state/telegram-agent/`;
6. сохраняет старый каталог и юниты как `~/.local/share/telegram-agent.v1-<время>` (внутри —
   `rollback.sh`), переносит новый чекаут в `~/.local/share/telegram-agent`;
7. запускает `install.sh` (юниты теперь указывают на чекаут);
8. перезапускает `telegram-mcp` (его юнит изменился) и `asr-server`, запускает бота и таймер и
   проверяет, что бот не упал за первые 20 с.

Старый `prompt.md` сохраняется в `~/.config/telegram-agent/prompt.v1.md` для справки — v2 читает
промпты из `agent/prompts/`. Откат: `~/.local/share/telegram-agent.v1-<время>/rollback.sh` — он
дожидается конца текущего ответа v2 и переносит в v1 курсоры чатов и память, чтобы ничего не
ответилось дважды и не забылось.

Из v1 в v2 убраны `guard.py`, `reflow.py` и хуки `settings.json`, открытые followup-поручения,
лента входящих (`TELEGRAM_EVENT_FEED`), `RESTART_STALLED_MCP`, Firecrawl, команда `/web`,
`WEB_SEARCH_PUBLIC_MODEL` и `OWNER_MCP_OUTPUT_TOKENS` — их роли теперь закрывают `broker.py`,
курсоры и история чата, а также встроенный `search_web`. Если v1 указывал на другой адрес MCP
(`mcp.json`), пропишите его в `MCP_URL` в `config.env`.

После миграции проверьте: личку владельца, личку гостя, обращение в группе, голосовое и
«запомни …». Следите за `journalctl --user -u telegram-agent -f`.

## Перенос на другую машину

1. На старой машине: `systemctl --user disable --now telegram-agent telegram-mcp`.
2. На новой: шаг 1 (`install.sh`).
3. Перенесите на новую машину безопасным способом (не через чат с ИИ):
   - `~/.local/share/telegram-mcp/.env` и `telegram_mcp_session.session`;
   - `~/.config/telegram-agent/config.env`, `backend.env` и `persona.md` (если меняли);
   - `~/.local/state/telegram-agent/memory.md`.
4. Если сменились пользователь или путь, поправьте `TELEGRAM_SESSION_NAME` в `.env`. Остальные
   пути вычисляются сами.
5. Шаги 5, 7, 9, 10, 11. Модель распознавания скачается заново при первом голосовом.

Можно и без переноса сессии: заново войти по шагу 4. Старую сессию затем завершите в Telegram:
«Устройства».

## Удаление

Остановите автоответчик после завершения текущего запроса (поставьте паузу и дождитесь
`pause.ack`, см. [OPERATIONS.md](OPERATIONS.md#команды)), затем выполните:

```bash
systemctl --user disable --now telegram-mcp-update.timer
systemctl --user stop telegram-mcp-update.service
systemctl --user disable --now telegram-agent telegram-mcp asr-server

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rm -f -- "$unit_dir"/{telegram-agent,telegram-mcp,asr-server,telegram-mcp-update}.service
rm -f -- "$unit_dir/telegram-mcp-update.timer"
systemctl --user daemon-reload
```

На телефоне завершите сессию аккаунта-бота в **Настройки → Устройства**. Уберите запись
`telegram` из MCP-конфигов, в которые добавляли её через шаг 8.

Остановка сохраняет конфигурацию и данные. Если они больше не нужны, удалите их отдельно.
Следующие команды удалят код, сессию, ключи, память и настройки. Сохраните нужные экспорты и
резервные копии заранее.

```bash
rm -r -- ~/.local/share/telegram-agent ~/.local/share/telegram-mcp
rm -r -- "${XDG_CONFIG_HOME:-$HOME/.config}/telegram-agent"
rm -r -- "${XDG_STATE_HOME:-$HOME/.local/state}/telegram-agent" "${XDG_STATE_HOME:-$HOME/.local/state}/telegram-mcp"
```

Каталог `~/TelegramMCP` с экспортами и `~/.cache/huggingface` (веса модели распознавания) удалите
при необходимости. `linger` можно выключить через `sudo loginctl disable-linger "$USER"`, если он
был включён только для пресета.
