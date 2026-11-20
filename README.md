# Telegram-MCP-preset

English · [Русский](README.ru.md)

This preset turns a regular Telegram account into an AI assistant. It replies to direct messages
and in selected groups, flags important conversations for the owner, and carries out the owner's
requests in Telegram.

The installer fetches [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) from
upstream and defaults to the verified tag `v3.3.23` for a fresh install. This repository supplies
the auto-responder, server configuration, local speech recognition, systemd units, and setup guides.

> Use a separate Telegram account for the assistant. The AI will act as that account.

## What it does

- **Shared Telegram MCP server.** One Telegram session serves compatible Streamable HTTP MCP
  clients at `http://127.0.0.1:8765/mcp`. This local endpoint exposes the upstream tool catalog.
- **Automatic replies.** The dispatcher batches eligible incoming messages and starts a fresh
  headless agent run. It handles DMs from people and responds in allowed groups when someone
  replies to the assistant, mentions it, or uses a configured trigger word. The owner can ask
  the assistant to manage the Telegram folder used for the group allowlist.
- **Owner and guest access.** The owner can ask the assistant to manage chats and publish
  summaries of long forum topics. Guests can access the current chat and can notify the owner
  or forward messages from that chat to the configured notification chat.
- **Memory and persona.** The owner can save instructions in `memory.md`. The default persona is
  the Russian-language Doom bot, styled after JARVIS. Set its name in `config.env` and edit
  `prompt.md` to change its language or style.
- **Local transcription.** The preset transcribes voice messages and video notes on the same
  machine using onnx-asr. The default model is full-precision GigaAM-v3 E2E RNN-T.
- **Upstream updates.** Enable `telegram-mcp-update.timer` to get weekly release notifications.
  Apply updates manually with `update_mcp.py apply`, which checks the result and rolls back on failure.

## How it works

```text
Telegram ⇄ telegram-mcp.service (one session, loopback HTTP)
             │ incoming DM feed and chat events
             ▼
           telegram-agent.service (dispatcher.py)
             │ fresh headless run for each eligible batch
             ▼
           Claude Code, Codex, OpenCode, or a custom runner
```

The main MCP server and the auto-responder run as systemd user services. The local ASR service
handles transcription. See [preset design and history (Russian)](docs/PRESET.md).

## Quick start

You need Linux with systemd user services, Python 3.10 or later, `git`, `uv`, and `ffmpeg`
for transcription. Have a separate Telegram account, Telegram API credentials, and access
to one of the supported agent CLIs or a custom runner.

```bash
git clone https://github.com/Plasmoid77/Telegram-MCP-preset
cd Telegram-MCP-preset
./install.sh
```

Follow the [installation guide (Russian)](docs/INSTALL.md) to enter the API credentials and sign in
to Telegram in your own terminal, start ASR and Telegram MCP, configure the account IDs and agent
backend, then start the auto-responder. The guide also covers
[running after logout and reboot](docs/INSTALL.md#10-работа-без-входа-в-систему-сервер-перезагрузки)
and [uninstalling](docs/INSTALL.md#удаление).
If an AI agent helps with setup, give it [AGENTS.md](AGENTS.md) and enter secrets yourself.

## Agent backends and models

Set `HARNESS` to `claude`, `codex`, `opencode`, or `command` and set `MODEL` in
`~/.local/share/telegram-agent/config.env`. Claude Code enforces tool permissions through hooks.
Codex, OpenCode, and custom runners use a short-lived local MCP gateway that checks tool calls
against the sender's role and current chat.

With OpenCode, the auto-responder uses the model specified by `MODEL` independently of the
interactive `/models` selection. The dispatcher reloads the file automatically, so changing the
model requires no restart.
See [model switching](docs/INSTALL.md#сменить-модель-автоответчика-opencode) and the
[backend configuration and custom runner guide (Russian)](docs/HARNESSES.md).

## Verified setups

| Setup | What was verified |
| --- | --- |
| **Claude Code on Arch Linux** | Live owner and guest DMs, group triggers and allowlist management, a 322-message forum topic summary, memory, and guard restrictions. [Test record (Russian)](docs/PRESET.md#5-проверено-2026-11-16) |
| **Codex and OpenCode CLIs** | Real CLIs with synthetic MCP tools. Reading and replying in the current chat worked, and access to another chat was blocked. OpenCode also passed owner tool discovery and invocation. [Integration tests](https://github.com/Plasmoid77/Telegram-MCP-preset/pull/1) |
| **OpenCode Go on LMDE 6** | Live Telegram replies in DMs with `opencode-go/qwen3.7-plus`, running as a systemd user service on a home server. |
| **Installer and unit removal on LMDE 6** | Fresh install and removal of systemd unit files exercised in a temporary home directory. [Test record](https://github.com/Plasmoid77/Telegram-MCP-preset/pull/1) |

CI runs ShellCheck, Python compilation, and the automated tests. Run the tests locally with
`python3 -m unittest discover -s tests`.

## Repository contents

| Path | Contents |
| --- | --- |
| [install.sh](install.sh) | Per-user installer. Reruns preserve credentials, the Telegram session, and existing `config.env` and `prompt.md`. |
| [agent/](agent/) | Dispatcher, backend adapters, MCP gateway, Claude Code hooks, prompt and configuration templates, and upstream updater. |
| [asr/](asr/) | Local speech recognition with an OpenAI-compatible transcription endpoint. |
| [systemd/](systemd/) | User units for Telegram MCP, the auto-responder, ASR, and release notifications. |
| [server/env.example](server/env.example) | Template for the upstream server's `.env`. |
| [docs/INSTALL.md](docs/INSTALL.md) | Fresh install, migration, and removal (Russian). |
| [docs/USAGE.md](docs/USAGE.md) | Operations, logs, troubleshooting, and updates (Russian). |
| [docs/PRESET.md](docs/PRESET.md) | Preset design, permission model, and history (Russian). |
| [docs/HARNESSES.md](docs/HARNESSES.md) | Backend configuration and custom runners (Russian). |
| [AGENTS.md](AGENTS.md) | Instructions for AI-assisted installation. |
| [tests/](tests/) | Dispatcher, gateway, hooks, updater, and ASR tests. |

## License

This preset is distributed under [GPL-3.0-or-later](LICENSE).
The installer fetches telegram-mcp separately under its
[Apache-2.0 license](https://github.com/chigwell/telegram-mcp/blob/v3.3.23/LICENSE).
