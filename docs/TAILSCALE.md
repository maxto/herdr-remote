# Reaching the relay over Tailscale

The relay listens on `127.0.0.1` and stays there. Tailscale publishes it under
a hostname that already carries a valid TLS certificate, which is what the
dashboard needs: a page served over HTTPS may only open a `wss://` socket, and
a certificate is the only way to get one without buying a domain.

This is an alternative to the Cloudflare tunnel described in
[QUICKSTART.md](../QUICKSTART.md). Pick one; running both only widens the
surface.

## Serve or Funnel

| | `tailscale serve` | `tailscale funnel` |
|---|---|---|
| Who can reach the relay | only your own devices | anyone on the internet |
| Tailscale on the phone | required | not needed |
| Address | `<machine>.<tailnet>.ts.net` | same |
| Certificate | valid, automatic | valid, automatic |

Serve keeps the relay off the public internet and is the safer default. Funnel
trades that away for a phone that needs nothing installed. Both give a fixed
address that survives reboots, which a quick Cloudflare tunnel does not.

Funnel is off until the tailnet policy grants it. In the admin console, under
**Access controls**, add the node attribute:

```json
"nodeAttrs": [
  { "target": ["100.x.y.z"], "attr": ["funnel"] }
]
```

Use the Tailscale IP of the machine running the relay. Granting the attribute
does not expose anything by itself; it only permits the command below.

## Setup

Run these on the machine hosting the relay. Under WSL, run them inside WSL:
`tailscaled` there is a separate node from the one on Windows, and only the
Linux node shares a loopback with the relay.

```bash
# 1. Publish the relay. Use `serve` for private, `funnel` for public.
tailscale funnel --bg 8375

# 2. Confirm the address
tailscale funnel status
```

Then tell the relay to trust that origin, in `~/.config/herdr-remote/config.env`:

```bash
HERDR_RELAY_TRUSTED_ORIGINS=https://<machine>.<tailnet>.ts.net
```

Restart the relay so it reads the change:

```bash
systemctl --user restart herdr-relay.service
```

The relay serves the dashboard itself, so one address covers both the page and
the socket. Open `https://<machine>.<tailnet>.ts.net` in a browser: the relay
URL fills itself in, and only the token has to be pasted. The browser remembers
the token for that address; it never appears in a URL.

## Why the origin still matters

A token alone does not identify the caller. A browser attaches a stored token
to any page that asks for it, including one the operator never chose to visit,
so the allowlist is what separates the dashboard from a drive-by. Configure it
even behind a token.

`HERDR_RELAY_TRUSTED_ORIGINS` is the documented name;
`HERDR_TRUSTED_ORIGINS` is accepted as an alias. With neither set, only
`localhost` and `127.0.0.1` origins are accepted.

## Verifying

From another machine, with no credentials:

```bash
curl -sI https://<machine>.<tailnet>.ts.net/ | head -1
```

The dashboard shell is public by design — it holds no agent state — so this
returns `200`. Every piece of agent data sits behind the WebSocket, which
closes an unauthenticated client with `1008`, and rejects an unlisted browser
origin with `403` before the upgrade completes.

## Turning it off

```bash
tailscale funnel --https=443 off   # stop publishing
tailscale serve reset              # drop the proxy configuration entirely
```

Removing the `funnel` node attribute revokes the capability regardless of what
runs on the machine.
