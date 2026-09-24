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

Selecting an agent opens its terminal in a dedicated view and hides the dashboard
header without requesting browser fullscreen. Only terminal output and a small
floating eye button are shown initially. Tap it to reveal the toolbar and command
field; tap the crossed-out eye to return to output only. Hiding controls closes
search and key panels, dismisses the keyboard and preserves any command draft.
Incoming updates keep your controls visible or hidden as you chose; opening a
terminal from the list starts with them hidden.

The terminal uses the available screen width and height on phones and tablets
in either orientation, including the area behind a display cutout. The output may
therefore pass behind the camera; the floating menu and revealed toolbar retain
safe-area spacing so their controls remain reachable.

When controls are visible, choose one input mode: **ABC** opens the Android
keyboard, **Keys** provides terminal keys such as Tab, Escape and Ctrl, **123**
provides a number pad, and **Commands** shows quick replies and agent commands.
Only one keyboard area is shown at a time. The command draft is preserved while
switching modes, and custom panels scroll when the available height is small.

Press **Send** to submit the text directly to the selected agent. The draft stays
visible until the relay confirms that the agent accepted it, and remains available
for retry after a connection or delivery error. Each pane keeps its own draft.

Output wraps to the screen width by default. **Keep columns** in the terminal
toolbar preserves original line layout for tables and structured output, with
horizontal scrolling. Both modes preserve ANSI colours and leave the terminal
on the host machine at its existing size.

The terminal fills the available app window and refreshes its output automatically
every three seconds and after input, so it has no manual refresh control. Scroll to
the top to load older pane output, up to 5,000 lines. The back arrow returns to the
agent list. System gestures and the keyboard can change the available viewport;
the terminal follows those changes without entering browser fullscreen.

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

## When Funnel stops answering

Funnel has twice gone dark while everything on this machine stayed healthy.
The phone shows `connecting…`, then `offline`, then retries, and the relay
logs no `Page request:` from the handset. The public Funnel ingress accepts
the TCP connection, then stalls after the TLS ClientHello and never forwards a
packet to the node.

Tell it apart from a local fault by forcing the public ingress, from the
machine that runs the relay:

```bash
H=<machine>.<tailnet>.ts.net
for IP in $(dig +short @1.1.1.1 $H A); do
  curl -s -o /dev/null -w "$IP %{http_code}\n" --max-time 8 --resolve $H:443:$IP https://$H/
done
```

Plain `curl https://$H/` on a tailnet machine with MagicDNS goes to the
tailnet address, not the ingress, so it answers `200` even during the outage.
If the probe times out while `curl http://127.0.0.1:8375/` answers, the fault
is the ingress. Re-register the Funnel:

```bash
tailscale funnel --https=443 off
tailscale funnel --bg --https=443 http://127.0.0.1:8375
```

It can take a minute or two for every ingress address to recover. Restarting
`tailscaled` was not needed.

| Date | Duration | Resolution |
|------|----------|------------|
| 2026-09-20 | about 7 h (14:39–21:35) | Healed on its own; cause not found |
| 2026-09-23/24 | about 21 h (from 22:16) | Funnel off/on; all ingresses answered within about 90 s |

Both times `tailscaled` had been running since 2026-09-20 09:58, and
status.tailscale.com reported no incident. Why the ingress loses the node is
still unknown. If it happens again, the next step is a continuity measure
(a watchdog that re-registers the Funnel, or a second path such as the
Cloudflare tunnel) rather than another manual fix.

## Turning it off

```bash
tailscale funnel --https=443 off   # stop publishing
tailscale serve reset              # drop the proxy configuration entirely
```

Removing the `funnel` node attribute revokes the capability regardless of what
runs on the machine.
