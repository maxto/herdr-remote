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

## Install on Android

After updating the repository, restart `herdr-relay.service` to load the routes
that serve the app manifest and icons. Updating only the HTML is not enough
when the running relay predates those routes.

Open your own dashboard's HTTPS address in Chrome, reload it, then choose
**Add to home screen → Install** from the three-dot menu. Launch **Herdr** from
the new icon: it opens in its own window without Chrome's address bar. Android's
status and navigation bars may remain visible.

Selecting an agent opens its terminal in a dedicated view, hides the dashboard
header and requests fullscreen. The terminal uses the available screen width
and height on phones and tablets in either orientation. Its command field stays
within the visible area when the on-screen keyboard reduces that space; extra
key panels scroll when space is limited.

Output wraps to the screen width by default. **Keep columns** in the terminal
toolbar preserves original line layout for tables and structured output, with
horizontal scrolling. Both modes preserve ANSI colours and leave the terminal
on the host machine at its existing size.

The back arrow returns to the agent list and leaves fullscreen. The terminal's
fullscreen button can toggle the browser mode without closing the terminal;
it updates if Android exits the mode. Fullscreen controls are shown only when
supported. If the browser refuses fullscreen, the dedicated terminal still
fills the available app window. System gestures and the keyboard can bring
Android controls back into view.

An older home-screen shortcut may still open a Chrome tab. Install from the
updated page and use the newly installed app; the old shortcut can then be
removed. If Chrome offers only a shortcut, verify that `/manifest.webmanifest`,
`/icons/icon-192.png` and `/icons/icon-512.png` return `200` on the same HTTPS
address without credentials. The demo linked in the upstream README is a
different deployment and does not receive this fork's changes.

The installation reuses the existing dashboard and relay. It still needs a
network connection; agent output is not cached for offline use.

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
