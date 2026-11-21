# Telegram-MCP-preset

English · [Русский](README.ru.md)

This preset turns a regular Telegram account into an AI assistant. It replies to direct messages
and in selected groups, flags important conversations for the owner, and carries out the owner's
requests in Telegram: reading and summarising chats, replying, forwarding, managing folders.

The installer fetches [chigwell/telegram-mcp](https://github.com/chigwell/telegram-mcp) from
upstream at the verified tag `v3.3.23`. This repository supplies the auto-responder, the server
configuration, local speech recognition, systemd units and the guides.

> Use a separate Telegram account for the assistant. The AI acts as that account.

## What it does

- **One Telegram session, shared.** telegram-mcp serves the account at `http://127.0.0.1:8765/mcp`
  for the auto-responder and for your own MCP clients.
- **Automatic replies.** The dispatcher answers every DM, and in allowed groups replies, mentions
  and trigger words, with one fresh headless agent run per settled burst of messages. Chat history
  is the source of truth: nothing is lost after a network drop and nothing is answered twice.
- **Owner and guests.** In the owner's DM the assistant can use the whole account. Any other run —
  a guest's DM or a group, even when the owner asks there — works only with its own chat and can
  notify the owner. The limit is enforced in code, not only in the prompt.
- **Rich media and the web.** Voice messages and video notes are transcribed locally (onnx-asr,
  GigaAM-v3 by default); photos, documents and videos can be inspected; public web search
  (DuckDuckGo, no key) works in every run, and runs the owner started can open web links
  (pages and images; public addresses only, only links already seen in the conversation).
- **Memory and persona.** The owner can ask the assistant to remember instructions. The default
  persona is the Russian-speaking «Doom bot» in the manner of JARVIS; replace it with your own
  `persona.md`.
- **Any agent CLI.** Claude Code, Codex, OpenCode or your own runner, chosen in `config.env`.
- **Upstream updates.** A weekly timer reports new telegram-mcp releases; `update_mcp.py apply`
  upgrades with a smoke test and automatic rollback.

## How it works

```text
Telegram ⇄ telegram-mcp.service (one session, loopback HTTP)
             ▲ wait_for_events + history reads
             │
           telegram-agent.service (agent/dispatcher.py)
             │ one run per settled burst
             ▼
           broker (agent/broker.py, a private per-run MCP URL; checks every call)
             ▲
             │ the only MCP server the agent sees
           Claude Code │ Codex │ OpenCode │ custom runner
```

The broker is the single place where access is decided ([agent/policy.py](agent/policy.py)):
the agent CLI starts with no built-in tools and no personal configuration, and talks only to it.
Design and security model: [docs/DESIGN.md](docs/DESIGN.md) (Russian).

## Quick start

Linux with systemd user services, Python 3.10+, `git`, `uv`, `ffmpeg`; a separate Telegram
account with API credentials; one supported agent CLI logged in.

```bash
git clone https://github.com/Plasmoid77/Telegram-MCP-preset ~/.local/share/telegram-agent
~/.local/share/telegram-agent/install.sh
```

Then follow [docs/INSTALL.md](docs/INSTALL.md) (Russian): Telegram credentials and login in your
own terminal, `~/.config/telegram-agent/config.env`, start the services. Upgrading an existing
v1 install: [migration](docs/INSTALL.md#переход-с-v1). If an AI agent helps with the setup, give it
[AGENTS.md](AGENTS.md) and enter secrets yourself.

## Files

| Where | What |
| --- | --- |
| `~/.local/share/telegram-agent/` | this repository (code; upgrade = `git checkout <tag>` + `./install.sh`) |
| `~/.config/telegram-agent/` | `config.env`, `backend.env` (agent credentials), optional `persona.md` |
| `~/.local/state/telegram-agent/` | chat cursors, the assistant's memory, pause flag |
| `~/.local/share/telegram-mcp/` | upstream server checkout, its `.env` and the Telegram session |

## Repository

| Path | Contents |
| --- | --- |
| [agent/](agent/) | dispatcher, broker + policy, agent adapters, web search, MCP client, updater, prompts, config templates |
| [asr/](asr/) | local speech recognition with an OpenAI-compatible endpoint |
| [systemd/](systemd/) | user units (rendered by `install.sh` with the checkout path) |
| [scripts/migrate-v1.sh](scripts/migrate-v1.sh) | move a v1 install to this layout, with rollback |
| [docs/](docs/) | [INSTALL](docs/INSTALL.md), [OPERATIONS](docs/OPERATIONS.md), [DESIGN](docs/DESIGN.md) (Russian) |
| [tests/](tests/) | unit tests and a contract test against the pinned upstream source |

## Verification

CI runs ShellCheck, the unit tests (policy, broker over HTTP, adapters, dispatcher, updater,
ASR decoding with real ffmpeg, the v1 migration and its rollback) and a contract test that every
tool and argument the preset uses exists in the pinned telegram-mcp release. Locally:
`python3 -m unittest discover -s tests`.

v1 of this preset was verified live with Claude Code (Arch Linux) and with OpenCode Go
`qwen3.7-plus` on a home server (LMDE 6).

## License

[GPL-3.0-or-later](LICENSE). The installer fetches telegram-mcp separately under its
[Apache-2.0 license](https://github.com/chigwell/telegram-mcp/blob/v3.3.23/LICENSE).
