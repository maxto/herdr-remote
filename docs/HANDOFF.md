# Handoff

Where this fork stands, and why it diverges from upstream. Written for whoever
picks the work up next, including a future session of an agent.

Host-specific values appear as placeholders. Real hostnames, tokens and keys
live in `~/.config/herdr-remote/` and never in this repository.

## What this fork is for

Upstream watches the agents of one herdr session. This fork aggregates **every
running local session** — each agent carries its session name, and pane ids are
namespaced (`session:workspace:pane`) because herdr's own ids are only unique
within a session.

Work happens on `main`. There is no upstream remote: the divergence below is
deliberate and is not waiting to be contributed back.

## How it is reached

```
browser ──https──► Tailscale funnel ──► relay (127.0.0.1:8375) ──► herdr CLI
```

The relay serves the dashboard itself at `/`, so one address covers both the
page and the WebSocket, and **saving a file in `web/` is the deploy**. There is
no build step and no Cloudflare Pages any more.

The dashboard has a standalone web app manifest and PNG icons for installation
from Android Chrome. The relay must be restarted after pulling the change that
adds their HTTP routes; later edits to the assets are read on request. See
the Android installation steps in `docs/TAILSCALE.md`.

Selecting a terminal opens a dedicated responsive view. `TerminalViewport` in
`web/index.html` follows the visual viewport, including keyboard changes, without
requesting the browser fullscreen API. Output wraps unless **Keep columns** is
enabled; no remote pane resize is sent. The same controller hides terminal chrome
on entry and owns the floating eye button for showing or hiding terminal controls.
Hiding controls closes panels and blurs the input without clearing its draft.
Pane output refreshes automatically every three seconds and after input; there is
no separate manual refresh button. Incoming updates preserve the controls state.

`TerminalInput` makes **ABC**, **Keys**, **123** and **Commands** mutually
exclusive. It measures the last native-keyboard height when Chromium exposes it,
and custom panels reuse that bounded space. In compact view, transient composer
status overlays output, image preview becomes a filename chip and terminal output
may shrink to 26px so input controls stay within the visual viewport.

`send_attachment` is an authenticated WebSocket operation. The browser sends one
PNG, JPEG or WebP image (5 MiB decoded maximum) with an optional 1000-character
message and waits for a request-scoped acknowledgement. The relay validates the
container, writes a generated owner-only file below its data directory, then runs
`herdr agent prompt` for the selected local pane/session with the absolute path.
SSH panes are rejected before writing. Storage stops accepting images at 100 files
or 50 MiB and never evicts an image an agent may still need. Override the parent
directory with `HERDR_RELAY_DATA_DIR`; the default is the platform's normal user
data location. The relay must restart after this protocol handler changes.

`docs/TAILSCALE.md` covers serve versus funnel and the node attribute that gates
funnel.

## Security model

Three independent gates, each verifiable from outside:

| Gate | Failure mode |
|---|---|
| Token, sent as an `auth` message after the socket opens | close `1008` |
| Browser origin allowlist, checked before the upgrade | HTTP `403` |
| Everything except the dashboard shell requires the token | HTTP `401` |

Decisions worth keeping:

- **The token never appears in a URL.** It travels in the handshake. Upstream
  appended `?token=` to the socket URL, which leaks into history, bookmarks and
  proxy logs — survivable on loopback, not behind a public tunnel.
- **The browser remembers the token, deliberately.** An earlier revision kept it
  in memory only; neither autofill nor the Credential Management API filled the
  field in practice, so every glance at the dashboard began by pasting 64
  characters. It is stored under `herdr_relay_token` and forgotten when the
  relay changes.
- **The origin allowlist applies even when a token is set.** Upstream skips the
  check in that case; a browser attaches a stored token to any page that asks,
  so the token cannot tell the operator's dashboard from a hostile one.
- **The allowlist is read under either variable name.** The advisory, installer
  and tests say `HERDR_RELAY_TRUSTED_ORIGINS`; upstream's code read
  `HERDR_TRUSTED_ORIGINS` (their issue #33). Both work here.
- **The dashboard shell is public.** The browser must load the page before it
  can authenticate, and demanding a token there would force the secret back into
  the URL. The shell holds no agent state.

## Status log

`$HERDR_LOG_DIR/timeline.jsonl`, owner-only, one JSON line per status change,
capped at 500 entries and rewritten whole so it cannot outgrow the cap. Reloaded
at startup, served over the authenticated socket as `get_timeline`.

Metadata only: session, project, agent, pane, ISO timestamp, status. Prompts and
pane output stay out of it. Rendered in the reader's local time.

The first poll after startup seeds the status map **silently** — with an empty
map every agent looks like it just changed, which used to write a burst of
invented transitions and would now push a notification per finished agent on
every restart.

## Notifications

Web push, VAPID keys in `config.env` (public) and `secrets.env` (private).
Without them the button in settings cannot work.

| Event | Tag |
|---|---|
| agent blocked, with the first 120 characters of the prompt | `herdr-blocked` |
| agent done — finished and not yet looked at | `herdr-done` |

The tag is also the FCM collapse key, so waiting and finished never overwrite
each other. Leaving either state clears its notification. `TTL: 21600` matters:
without it a sleeping phone never receives anything.

The Telegram bot was removed. Nothing in the code prevents bringing it back —
it is configuration-driven — but the credentials and its service are gone.

## Conventions

- `AGENTS.md` holds the workflow: present a plan, stop, then carry the approved
  task through to the end.
- Tests live in `tests/`, run with `sh tests/run.sh`. Behaviour that mattered
  enough to debug once has a test; copy does not.
- Optional layout checks use Playwright and synthetic data:
  `HERDR_BROWSER_TESTS=1 sh tests/run.sh`, after
  installing its Chromium with
  `uv run --with playwright python -m playwright install chromium --only-shell`.
  They exercise phone/tablet sizes, wrapping and constrained keyboard
  space. Actual Android keyboard and system-bar behaviour still needs a device.
- Never write credentials, tokens, personal hostnames or absolute home paths
  into this repository.

## Known rough edges

- `demo-worker/`, `herdi-mac/` and `herdi-ios/` still point at upstream and have
  not been touched.
- `herdi-mac`'s updater checks upstream's releases, and a test asserts it.
- The dashboard has no theme picker; it follows the system light/dark setting.
