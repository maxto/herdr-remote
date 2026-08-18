# WebSocket Authentication Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove relay tokens from WebSocket URLs while preserving authenticated web, Telegram, and TUI access.

**Architecture:** A shared Python client helper normalizes legacy relay URLs and performs a versioned first-message authentication exchange. The relay validates browser origins during upgrade, authenticates before registering a client or sending state, and retains Bearer authentication for HTTP. The web client uses the same protocol with an in-memory-only token and executes no third-party JavaScript.

**Tech Stack:** Python 3.10+, `websockets>=14.0`, JavaScript, Node.js assertions, `unittest`, shell installer tests, Cloudflare Pages.

**Spec:** `docs/superpowers/specs/2026-08-18-websocket-auth-design.md`

## Global Constraints

- Never print, log, commit, or return a real relay token.
- Protocol version is exactly `1`.
- Authentication timeout is exactly five seconds.
- Failed authentication closes with code `1008` and reason `Unauthorized`.
- Browser trusted origins are exact matches from `HERDR_RELAY_TRUSTED_ORIGINS`.
- Non-browser clients without an `Origin` header remain eligible for token authentication.
- `HERDR_RELAY` is token-free; `HERDR_RELAY_TOKEN` remains in the mode-`0600` secrets file.
- Do not add dependencies.
- Do not start a public tunnel during implementation or verification.
- Push only `feat/multi-session-local` to the `personal` remote (`maxto/herdr-remote`).

---

### Task 1: Shared Python WebSocket authentication client

**Files:**
- Create: `relay/ws_auth.py`
- Create: `tests/test_ws_auth.py`

**Interfaces:**
- Produces: `normalize_relay_credentials(relay_url: str, explicit_token: str) -> tuple[str, str]`
- Produces: `authenticate_websocket(ws, token: str, timeout: float = 5) -> None`
- Produces: `authenticated_connection(relay_url: str, token: str)` async context manager
- Produces: `RelayAuthenticationError(RuntimeError)`

- [ ] **Step 1: Write failing URL-migration tests**

```python
from relay.ws_auth import normalize_relay_credentials


def test_explicit_token_wins_and_url_never_contains_token():
    url, token = normalize_relay_credentials(
        "wss://relay.example/socket?token=legacy&view=agents",
        "dedicated-secret",
    )
    assert url == "wss://relay.example/socket?view=agents"
    assert token == "dedicated-secret"
    assert "legacy" not in url
    assert "dedicated-secret" not in url


def test_legacy_query_token_migrates_to_memory():
    url, token = normalize_relay_credentials(
        "ws://127.0.0.1:8375?token=legacy-secret",
        "",
    )
    assert url == "ws://127.0.0.1:8375"
    assert token == "legacy-secret"
```

- [ ] **Step 2: Run the tests and observe the missing-module failure**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_ws_auth -v`

Expected: FAIL because `relay.ws_auth` does not exist.

- [ ] **Step 3: Implement URL normalization without logging secrets**

```python
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_relay_credentials(relay_url: str, explicit_token: str) -> tuple[str, str]:
    parts = urlsplit(relay_url)
    clean_query = []
    legacy_token = ""
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "token":
            legacy_token = legacy_token or value
        else:
            clean_query.append((key, value))
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(clean_query), parts.fragment))
    return clean_url, explicit_token or legacy_token
```

- [ ] **Step 4: Add failing handshake tests**

```python
import json
import unittest

from relay.ws_auth import RelayAuthenticationError, authenticate_websocket


class FakeSocket:
    def __init__(self, response):
        self.response = response
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(self.response)


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_sends_versioned_first_message(self):
        ws = FakeSocket({"type": "auth_result", "protocol": 1, "ok": True})
        await authenticate_websocket(ws, "secret")
        self.assertEqual(ws.sent, [{"type": "auth", "protocol": 1, "token": "secret"}])

    async def test_authentication_rejects_invalid_acknowledgement(self):
        ws = FakeSocket({"type": "error"})
        with self.assertRaises(RelayAuthenticationError):
            await authenticate_websocket(ws, "secret")
```

- [ ] **Step 5: Run the handshake tests and observe the missing-symbol failure**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_ws_auth -v`

Expected: FAIL because the authentication symbols do not exist.

- [ ] **Step 6: Implement the shared handshake and connection context**

```python
import asyncio
from contextlib import asynccontextmanager
import json


AUTH_PROTOCOL = 1


class RelayAuthenticationError(RuntimeError):
    pass


async def authenticate_websocket(ws, token: str, timeout: float = 5) -> None:
    if not token:
        return
    await ws.send(json.dumps({"type": "auth", "protocol": AUTH_PROTOCOL, "token": token}))
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    response = json.loads(raw)
    expected = {"type": "auth_result", "protocol": AUTH_PROTOCOL, "ok": True}
    if response != expected:
        raise RelayAuthenticationError("relay authentication failed")


@asynccontextmanager
async def authenticated_connection(relay_url: str, token: str):
    import websockets
    async with websockets.connect(relay_url) as ws:
        await authenticate_websocket(ws, token)
        yield ws
```

- [ ] **Step 7: Run the focused tests**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_ws_auth -v`

Expected: all `tests.test_ws_auth` tests PASS.

- [ ] **Step 8: Commit the helper**

```bash
rtk git add relay/ws_auth.py tests/test_ws_auth.py
rtk git commit -m "feat(auth): add shared websocket client handshake"
```

---

### Task 2: Relay authentication gate and trusted browser origins

**Files:**
- Modify: `relay/herdr_relay.py:1-80`
- Modify: `relay/herdr_relay.py:879-920`
- Modify: `relay/herdr_relay.py:1012-1045`
- Modify: `tests/test_herdr_relay.py`

**Interfaces:**
- Consumes: protocol value `1` from the approved spec
- Produces: `authenticate_client(ws) -> bool`
- Produces: `origin_is_allowed(origin: str) -> bool`
- Produces: `TRUSTED_ORIGINS: set[str]`

- [ ] **Step 1: Complete failing relay tests**

Update the existing success message to include protocol 1 and add literal failure cases:

```python
ws = _FakeWebSocket([
    json.dumps({"type": "auth", "protocol": 1, "token": "correct-secret"})
], headers={"Origin": self.ORIGIN})

self.assertEqual(
    json.loads(ws.sent[0]),
    {"type": "auth_result", "protocol": 1, "ok": True},
)

for bad_message in (
    "not-json",
    json.dumps({"type": "auth", "protocol": 2, "token": "correct-secret"}),
    json.dumps({"type": "auth", "protocol": 1, "token": "wrong-secret"}),
):
    with self.subTest(bad_message=bad_message):
        ws = _FakeWebSocket([bad_message], headers={"Origin": self.ORIGIN})
        with mock.patch.object(relay, "send_current_snapshot", new=mock.AsyncMock()) as snapshot:
            asyncio.run(relay.handle_client(ws))
        self.assertEqual(ws.closed, (1008, "Unauthorized"))
        snapshot.assert_not_awaited()
```

Add an authorized-origin upgrade assertion:

```python
request = types.SimpleNamespace(
    path="/",
    headers=_Headers({"Upgrade": "websocket", "Origin": self.ORIGIN}),
)
self.assertIsNone(asyncio.run(relay.process_request(None, request)))
```

Add timeout and no-token compatibility tests:

```python
class _TimeoutWebSocket(_FakeWebSocket):
    async def recv(self):
        raise asyncio.TimeoutError


def test_authentication_timeout_closes_without_snapshot(self):
    with loaded_relay(relay_token="correct-secret", trusted_origins=self.ORIGIN) as relay:
        ws = _TimeoutWebSocket([], headers={"Origin": self.ORIGIN})
        with mock.patch.object(relay, "send_current_snapshot", new=mock.AsyncMock()) as snapshot:
            asyncio.run(relay.handle_client(ws))
        self.assertEqual(ws.closed, (1008, "Unauthorized"))
        self.assertEqual(ws.sent, [])
        snapshot.assert_not_awaited()


def test_no_token_preserves_local_snapshot_behavior(self):
    with loaded_relay() as relay:
        ws = _FakeWebSocket([])
        with mock.patch.object(relay, "send_current_snapshot", new=mock.AsyncMock()) as snapshot:
            asyncio.run(relay.handle_client(ws))
        snapshot.assert_awaited_once_with(ws)
        self.assertEqual(ws.sent, [])
```

Add a non-WebSocket HTTP characterization test:

```python
def test_http_bearer_authentication_is_preserved(self):
    with loaded_relay(relay_token="correct-secret") as relay:
        accepted = types.SimpleNamespace(
            path="/private",
            headers=_Headers({"Authorization": "Bearer correct-secret"}),
        )
        rejected = types.SimpleNamespace(
            path="/private",
            headers=_Headers({"Authorization": "Bearer wrong-secret"}),
        )
        self.assertEqual(asyncio.run(relay.process_request(None, accepted)).status_code, 404)
        self.assertEqual(asyncio.run(relay.process_request(None, rejected)).status_code, 401)
```

This protects the HTTP Bearer contract while WebSocket handling is reordered.

- [ ] **Step 2: Run relay authentication tests and verify RED**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_herdr_relay.RelayAuthenticationTests -v`

Expected: FAIL because snapshots precede authentication, invalid auth is not closed, and attacker origin returns the old response.

- [ ] **Step 3: Add constants and exact origin parsing**

```python
import hmac

AUTH_PROTOCOL = 1
AUTH_TIMEOUT_SECONDS = 5
TRUSTED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("HERDR_RELAY_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
}

if AUTH_TOKEN and not TRUSTED_ORIGINS:
    log.warning("Relay token is enabled without trusted browser origins")


def origin_is_allowed(origin: str) -> bool:
    return not origin or not TRUSTED_ORIGINS or origin.rstrip("/") in TRUSTED_ORIGINS
```

- [ ] **Step 4: Let WebSocket upgrades reach the message-level gate**

In `process_request`, detect `Upgrade: websocket` before HTTP token validation. For upgrades, return HTTP 403 with body `b"Origin not allowed\n"` when `origin_is_allowed()` is false; otherwise return `None`. Keep the existing Bearer/query logic only for non-WebSocket HTTP requests.

- [ ] **Step 5: Authenticate before client registration or snapshots**

```python
async def authenticate_client(ws) -> bool:
    if not AUTH_TOKEN:
        return True
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=AUTH_TIMEOUT_SECONDS)
        message = json.loads(raw)
        valid = (
            isinstance(message, dict)
            and message.get("type") == "auth"
            and message.get("protocol") == AUTH_PROTOCOL
            and isinstance(message.get("token"), str)
            and hmac.compare_digest(message["token"], AUTH_TOKEN)
        )
    except (asyncio.TimeoutError, json.JSONDecodeError, ConnectionClosedError, ConnectionClosedOK):
        valid = False
    if not valid:
        await ws.close(code=1008, reason="Unauthorized")
        return False
    await ws.send(json.dumps({"type": "auth_result", "protocol": AUTH_PROTOCOL, "ok": True}))
    return True
```

Call this at the start of `handle_client`; return immediately on `False`. Only then add to `clients`, log connection metadata, and send the initial snapshot.

- [ ] **Step 6: Run focused relay tests GREEN**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_herdr_relay.RelayAuthenticationTests -v`

Expected: all authentication and origin tests PASS.

- [ ] **Step 7: Run all relay behavior tests**

Run: `rtk uv run --with 'websockets>=14.0' python -m unittest tests.test_herdr_relay -v`

Expected: all relay tests PASS; existing no-token tests retain their behavior.

- [ ] **Step 8: Commit the relay gate**

```bash
rtk git add relay/herdr_relay.py tests/test_herdr_relay.py
rtk git commit -m "feat(auth): gate websocket clients before relay access"
```

---

### Task 3: Migrate Telegram and TUI to the shared handshake

**Files:**
- Modify: `relay/herdr_telegram.py:20-35`
- Modify: `relay/herdr_telegram.py:90-165`
- Modify: `relay/herdr_telegram.py:958-990`
- Modify: `relay/herdr_tui.py:1-22`
- Modify: `relay/herdr_tui.py:168-205`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_ws_auth.py`

**Interfaces:**
- Consumes: `normalize_relay_credentials()` and `authenticated_connection()` from Task 1
- Produces: clean `RELAY_WS` and in-memory `RELAY_TOKEN` in both clients

- [ ] **Step 1: Add failing Telegram connection test**

Configure the real shared context to consume an auth acknowledgement before the Telegram command:

```python
connection = FakeRelayConnection([
    {"type": "auth_result", "protocol": 1, "ok": True},
    {"type": "command_result", "command": "respond", "ok": True},
])
old_url, old_token = tg.RELAY_WS, tg.RELAY_TOKEN
tg.RELAY_WS, tg.RELAY_TOKEN = "ws://127.0.0.1:8375", "relay-secret"
try:
    with patch("websockets.connect", return_value=connection):
        await tg.send_to_relay("w1:p1", "yes", prompt_id="prompt-1")
finally:
    tg.RELAY_WS, tg.RELAY_TOKEN = old_url, old_token

self.assertEqual(connection.sent[0], {
    "type": "auth", "protocol": 1, "token": "relay-secret"
})
self.assertEqual(connection.sent[1]["type"], "respond")
```

Add a response `{"type":"auth_result","protocol":1,"ok":false}` and assert `send_to_relay()` raises `RelayAuthenticationError` while `connection.sent` contains only the auth message.

- [ ] **Step 2: Run Telegram tests RED**

Run: `rtk uv run --with 'python-telegram-bot>=21.0' --with 'websockets>=14.0' python -m unittest tests.test_telegram -v`

Expected: the new tests FAIL because Telegram still opens `RELAY_WS` directly.

- [ ] **Step 3: Normalize Telegram configuration once**

```python
from ws_auth import authenticated_connection, normalize_relay_credentials

RELAY_WS, RELAY_TOKEN = normalize_relay_credentials(
    os.environ.get("HERDR_RELAY", "ws://127.0.0.1:8375"),
    os.environ.get("HERDR_RELAY_TOKEN", ""),
)
RELAY_WS_SAFE = RELAY_WS
```

Include `RELAY_TOKEN` and the Telegram bot token in `scrub()` without ever including a raw relay URL containing a query token.

- [ ] **Step 4: Replace every Telegram WebSocket context**

Replace every `async with websockets.connect(RELAY_WS) as ws:` with:

```python
async with authenticated_connection(RELAY_WS, RELAY_TOKEN) as ws:
```

Set `relay_connected = True` only after entering the authenticated context.

- [ ] **Step 5: Migrate TUI configuration and connection lifecycle**

Use the same normalization call and replace its connection context with `authenticated_connection(RELAY_WS, RELAY_TOKEN)`. Set the reactive `connected` state only after the authenticated context has been entered. Display only the clean `RELAY_WS`.

- [ ] **Step 6: Run Python-client tests**

Run: `rtk uv run --with 'python-telegram-bot>=21.0' --with 'websockets>=14.0' python -m unittest tests.test_ws_auth tests.test_telegram -v`

Expected: all shared-auth and Telegram tests PASS.

- [ ] **Step 7: Parse-check the TUI**

Run: `rtk python3 -c "import ast, pathlib; ast.parse(pathlib.Path('relay/herdr_tui.py').read_text(encoding='utf-8'))"`

Expected: exit 0.

- [ ] **Step 8: Commit Python client migration**

```bash
rtk git add relay/herdr_telegram.py relay/herdr_tui.py tests/test_telegram.py tests/test_ws_auth.py
rtk git commit -m "feat(auth): authenticate Telegram and TUI connections"
```

---

### Task 4: Browser memory-only authentication and script isolation

**Files:**
- Modify: `web/security.js`
- Modify: `web/index.html:1-20`
- Modify: `web/index.html:375-545`
- Modify: `tests/test_web_security.js`
- Create: `tests/test_web_assets.py`

**Interfaces:**
- Produces: `createAuthenticatedConnection(url: string, token: string) -> {url, authMessage}`
- Produces: `clearLegacyRelayToken(storage) -> void`

- [ ] **Step 1: Run the existing new web tests RED**

Run: `rtk node tests/test_web_security.js`

Expected: FAIL because `createAuthenticatedConnection` does not exist.

Run: `rtk python3 -m unittest tests.test_web_assets -v`

Expected: FAIL listing `https://esm.sh/cuelume@0.1.2` as an executable external source.

- [ ] **Step 2: Implement pure security helpers**

```javascript
function createAuthenticatedConnection(url, token) {
  return {
    url,
    authMessage: { type: 'auth', protocol: 1, token },
  };
}

function clearLegacyRelayToken(storage) {
  storage.removeItem('herdr_relay_token');
}
```

Export both through the existing `HerdrSecurity` API.

- [ ] **Step 3: Remove executable third-party JavaScript**

Delete the inline module that imports `https://esm.sh/cuelume@0.1.2`. Existing cue calls are already guarded with `if (window.cue)` and require no replacement dependency.

- [ ] **Step 4: Make the token ephemeral in the browser**

At startup:

```javascript
HerdrSecurity.clearLegacyRelayToken(localStorage);
let relayToken = '';
document.getElementById('relayToken').value = '';
```

In `saveAndConnect`, copy the trimmed password into `relayToken`, immediately clear the input, and never write it to Web Storage. Clear `relayToken` when switching sessions and when selecting the demo.

- [ ] **Step 5: Send auth after opening a clean URL**

Build `{url, authMessage}` with `createAuthenticatedConnection` and use this lifecycle:

```javascript
const connection = HerdrSecurity.createAuthenticatedConnection(url, relayToken);
ws = new WebSocket(connection.url);
ws.onopen = () => {
  if (relayToken) {
    setStatus('authenticating');
    ws.send(JSON.stringify(connection.authMessage));
  } else {
    setStatus('connected');
  }
};
ws.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.type === 'auth_result') {
    if (message.protocol === 1 && message.ok === true) setStatus('connected');
    return;
  }
  handleMessage(message);
};
```

Do not append any token to `connection.url`. Tokenless demo/local connections retain the existing `onopen` status transition.

- [ ] **Step 6: Run web tests GREEN**

Run: `rtk node tests/test_web_security.js`

Run: `rtk python3 -m unittest tests.test_web_assets -v`

Expected: both commands PASS.

- [ ] **Step 7: Commit browser hardening**

```bash
rtk git add web/security.js web/index.html tests/test_web_security.js tests/test_web_assets.py
rtk git commit -m "fix(web): keep relay auth out of URLs and storage"
```

---

### Task 5: Installer migration and operator documentation

**Files:**
- Modify: `relay/install-service.sh:340-380`
- Modify: `relay/install-service.sh` configuration-writing block
- Modify: `tests/install-service.sh`
- Modify: `README.md`
- Modify: `QUICKSTART.md`

**Interfaces:**
- Consumes: `HERDR_RELAY_TRUSTED_ORIGINS` as a comma-separated non-secret value
- Produces: token-free `HERDR_RELAY` and separate `HERDR_RELAY_TOKEN`

- [ ] **Step 1: Add failing installer assertions**

After a fresh install, assert:

```bash
assert_contains "$MAC_HOME/.config/herdr-remote/secrets.env" 'HERDR_RELAY=ws://127.0.0.1:8375'
assert_not_contains "$MAC_HOME/.config/herdr-remote/secrets.env" 'HERDR_RELAY=.*token='
assert_contains "$MAC_HOME/.config/herdr-remote/secrets.env" 'HERDR_RELAY_TOKEN='
```

Run one install with `HERDR_RELAY_TRUSTED_ORIGINS=https://herdr-remote-bfd.pages.dev` and assert the exact line is written to `config.env`, not `secrets.env`.

- [ ] **Step 2: Run installer tests RED**

Run: `rtk bash tests/install-service.sh`

Expected: FAIL because the installer still appends `?token=` and does not persist trusted origins.

- [ ] **Step 3: Stop composing token-bearing relay URLs**

Set only:

```bash
HERDR_RELAY_TOKEN="$RELAY_TOKEN"
HERDR_RELAY="ws://127.0.0.1:$WS_PORT"
RELAY_TRUSTED_ORIGINS="${HERDR_RELAY_TRUSTED_ORIGINS:-}"
```

Write `HERDR_RELAY_TRUSTED_ORIGINS` into `config.env`. Keep `HERDR_RELAY_TOKEN` and the clean `HERDR_RELAY` in `secrets.env` with its existing mode `0600`.

- [ ] **Step 4: Update usage documentation**

Document that browser tokens are entered separately, are forgotten on refresh, and must never be added to Pages URLs or relay URLs. Document `HERDR_RELAY_TRUSTED_ORIGINS` with the exact production Pages-origin example. Keep HTTP Bearer authentication examples token-redacted.

- [ ] **Step 5: Run installer and documentation checks**

Run: `rtk bash tests/install-service.sh`

Run: `rtk rg -n "\?token=|token=.*pages.dev" README.md QUICKSTART.md relay/install-service.sh`

Expected: installer tests PASS; any remaining `?token=` occurrence is an explicitly documented legacy migration note, not generated configuration.

- [ ] **Step 6: Commit installer migration**

```bash
rtk git add relay/install-service.sh tests/install-service.sh README.md QUICKSTART.md
rtk git commit -m "fix(auth): separate relay URLs from credentials"
```

---

### Task 6: Full verification, personal publication, and local configuration

**Files:**
- Verify: all files changed in Tasks 1-5
- Runtime configuration: `/home/maxto/.config/herdr-remote/config.env` (not committed)

**Interfaces:**
- Consumes: deployed Pages origin `https://herdr-remote-bfd.pages.dev`
- Produces: verified fork deployment and locally configured trusted origin

- [ ] **Step 1: Run repository integrity checks**

Run: `rtk git diff --check`

Run: `rtk bash -n relay/install-service.sh`

Run: `rtk python3 -m compileall -q relay tests`

Expected: every command exits 0.

- [ ] **Step 2: Run the complete project suite**

Run: `rtk bash tests/run.sh`

Expected: exit 0 with zero failed checks. Existing `datetime.utcnow()` deprecation warnings may remain but no new warnings are accepted.

- [ ] **Step 3: Audit the diff and secret boundaries**

Run: `rtk git status --short`

Run: `rtk git diff --stat personal/feat/multi-session-local...HEAD`

Run: `rtk rg -n "HERDR_RELAY_TOKEN=.{16,}|token=[A-Za-z0-9_-]{16,}" web relay tests docs README.md QUICKSTART.md`

Expected: only intended authentication files are committed and the secret scan finds no real credential values.

- [ ] **Step 4: Push only the personal feature branch**

Run: `rtk git push personal feat/multi-session-local`

Expected: push targets `maxto/herdr-remote`, not `origin`.

- [ ] **Step 5: Verify the Pages deployment**

Run:

```bash
rtk curl -fsSL https://herdr-remote-bfd.pages.dev/ -o /tmp/herdr-pages-index.html
rtk curl -fsSL https://herdr-remote-bfd.pages.dev/security.js -o /tmp/herdr-pages-security.js
rtk rg -n "esm\.sh|localStorage\.setItem\('herdr_relay_token'|token=.*WebSocket" /tmp/herdr-pages-index.html
rtk rg -n "createAuthenticatedConnection|clearLegacyRelayToken" /tmp/herdr-pages-security.js
```

Expected: the first `rg` has no matches; the second finds both exported helpers. Do not use or transmit a real token in this check.

- [ ] **Step 6: Configure the exact trusted origin locally**

After an explicit filesystem approval, add or replace this non-secret line in `/home/maxto/.config/herdr-remote/config.env`:

```text
HERDR_RELAY_TRUSTED_ORIGINS=https://herdr-remote-bfd.pages.dev
```

Do not read, rewrite, or display values from `secrets.env`. Verify `config.env` remains owned by the user and is not group/world writable.

- [ ] **Step 7: Restart and verify the local relay**

Run: `rtk systemctl --user restart herdr-relay.service`

Run: `rtk systemctl --user is-active herdr-relay.service`

Expected: `active`. Inspect recent service logs only through redacted patterns; do not print environment values or authentication messages.

- [ ] **Step 8: Stop before public exposure**

Report test, commit, push, deployment, and local-service evidence. Ask for a new explicit authorization before starting `cloudflared tunnel --url http://127.0.0.1:8375`. Do not create a tunnel as part of this task.
