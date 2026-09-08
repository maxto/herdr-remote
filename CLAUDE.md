# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

herdr-remote is a multi-client system for monitoring and approving [herdr](https://herdr.dev) AI agents remotely. It provides a WebSocket relay that bridges the herdr CLI with phone, desktop, Telegram, and terminal clients.

## Architecture

```
Clients (web/mac/ios/telegram/tui)
        │ WebSocket
        ▼
   relay (:8375)  ←── Cloudflare tunnel (public wss://)
        │
        ▼
   herdr CLI (local or SSH to HERDR_REMOTES)
```

The relay (`relay/herdr_relay.py`) is the central hub: it polls herdr for agent state, accepts push events via HTTP POST and UDP, and broadcasts to connected WebSocket clients. Clients send `respond`, `agent_prompt`, `read_pane`, `send_keys`, and `send_text` messages back through the relay to control agents.

## Components

| Path | What | Language |
|------|------|----------|
| `relay/herdr_relay.py` | WebSocket+HTTP relay server | Python (websockets, zeroconf) |
| `relay/herdr_telegram.py` | Telegram bot client | Python (python-telegram-bot) |
| `relay/herdr_tui.py` | Terminal TUI client | Python (textual) |
| `web/index.html` | Mobile/desktop web app (single file) | HTML/CSS/JS |
| `demo-worker/` | Cloudflare Worker mock relay for demos | JS |
| `herdi-mac/` | macOS menu bar app | Swift (SPM) |
| `herdi-ios/` | iOS app with widgets + Live Activities | Swift (XcodeGen) |

## Running Components

All Python scripts use [PEP 723 inline metadata](https://peps.python.org/pep-0723/) — `uv run` handles dependency installation automatically.

```bash
# Relay (main server)
uv run relay/herdr_relay.py

# Full setup with Cloudflare tunnel
relay/start.sh

# Telegram bot
HERDI_TG_TOKEN="..." HERDI_TG_CHAT_ID="..." uv run relay/herdr_telegram.py

# Terminal TUI
uv run relay/herdr_tui.py

# Demo worker (Cloudflare)
cd demo-worker && npx wrangler dev

# macOS app
cd herdi-mac && ./build.sh

# iOS app (generate Xcode project)
cd herdi-ios && xcodegen generate
```

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `HERDR_RELAY_PORT` | Relay WebSocket port (default: 8375) |
| `HERDR_RELAY_TOKEN` | Optional shared secret for auth |
| `HERDR_REMOTES` | Comma-separated SSH targets to poll |
| `HERDR_BIN` | Path to herdr binary (default: `/opt/homebrew/bin/herdr`) |
| `HERDR_RELAY` | Relay URL used by clients (default: `ws://127.0.0.1:8375`) |
| `HERDR_RELAY_TRUSTED_ORIGINS` | Comma-separated browser origins allowed to open a socket (alias: `HERDR_TRUSTED_ORIGINS`) |
| `HERDR_VAPID_PUBLIC` / `HERDR_VAPID_PRIVATE` | Web push signing keys; without them the push button cannot work |

## Web App

The web app is a single self-contained HTML file (`web/index.html`) plus `web/security.js`, with inline CSS and JS — no build step. The relay serves it directly, so a saved file is live on reload. It follows the system light/dark setting and includes a mobile terminal keyboard, PWA support with web push, and agent-icon detection.

## WebSocket Protocol

Messages are JSON with a `type` field:

A token-protected relay authenticates first: the client sends `auth`
(`{type, protocol, token}`) and gets `auth_result` before anything else. An
unauthenticated socket is closed with `1008` and receives no agent data. A
browser origin outside the allowlist is refused with `403` before the upgrade.

**Server → Client:** `agents` (complete state snapshot), `agent_update` (single-pane state merge), `blocked` (approval prompt), `pane_content` (terminal read), `timeline` (status log), `command_result` / `error` (request result)

**Client → Server:** `respond` (answer a blocked agent), `agent_prompt` (submit text to an agent), `read_pane` (request terminal content), `send_keys` (send key sequences), `send_text` (raw text without newline), `get_timeline` (status log), `push_subscribe` (register for web push)

### Attachments

A file goes up in chunks rather than in one message, and the relay hands the
agent a path — never the bytes. Images, PDFs and UTF-8 text are accepted, each
with its own ceiling.

| Message | What it carries |
|---------|-----------------|
| `attachment_begin` | `name`, `mime`, `size`, `pane_id` — the relay judges type, ceiling, quota and pane **before a byte arrives**, and answers with an `upload_id` |
| `attachment_chunk` | `upload_id`, `index`, base64 `data` — indices are strictly sequential |
| `attachment_commit` | `upload_id` and optional `text` — the relay verifies the total and the content, then prompts the agent |
| `attachment_abort` | `upload_id` — releases the file, the slot and the budget |

Each chunk's `command_result` is the flow control: browsers buffer rather than
block on send, so the client waits for one before sending the next. That keeps
every frame small, which is why `WS_MAX_SIZE` is 512 KiB rather than large
enough to hold a whole file.

An upload belongs to its connection. There is no resume: a dropped socket, an
idle sender, or a restart abandons it and removes the temporary.

## Deployment

- Web app: served by the relay itself at `/`; editing `web/` is the deploy
- Remote access: Tailscale serve or funnel (see [docs/TAILSCALE.md](docs/TAILSCALE.md)), or a Cloudflare tunnel
- Demo worker: `npx wrangler deploy` from `demo-worker/`
- macOS app: `herdi-mac/build.sh` produces `dist/Herdi.app`

## Status log

The relay appends one JSON line per agent status change to
`$HERDR_LOG_DIR/timeline.jsonl` (owner-only, capped at 500 entries, reloaded at
startup). It holds metadata only — session, project, agent, pane, ISO
timestamp, status. Prompts and pane output never go there.
