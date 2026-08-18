import json
import unittest

from relay.ws_auth import RelayAuthenticationError, authenticate_websocket, normalize_relay_credentials


class RelayCredentialNormalizationTests(unittest.TestCase):
    def test_explicit_token_wins_and_url_never_contains_token(self):
        url, token = normalize_relay_credentials(
            "wss://relay.example/socket?token=legacy&view=agents",
            "dedicated-secret",
        )
        self.assertEqual(url, "wss://relay.example/socket?view=agents")
        self.assertEqual(token, "dedicated-secret")
        self.assertNotIn("legacy", url)
        self.assertNotIn("dedicated-secret", url)

    def test_legacy_query_token_migrates_to_memory(self):
        url, token = normalize_relay_credentials(
            "ws://127.0.0.1:8375?token=legacy-secret",
            "",
        )
        self.assertEqual(url, "ws://127.0.0.1:8375")
        self.assertEqual(token, "legacy-secret")


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
