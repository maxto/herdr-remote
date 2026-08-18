"""Shared WebSocket relay authentication helpers."""

import asyncio
from contextlib import asynccontextmanager
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


AUTH_PROTOCOL = 1


class RelayAuthenticationError(RuntimeError):
    pass


def normalize_relay_credentials(relay_url: str, explicit_token: str) -> tuple[str, str]:
    parts = urlsplit(relay_url)
    clean_query = []
    legacy_token = ""
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "token":
            legacy_token = legacy_token or value
        else:
            clean_query.append((key, value))
    clean_url = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(clean_query), parts.fragment)
    )
    return clean_url, explicit_token or legacy_token


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
