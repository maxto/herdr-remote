from html.parser import HTMLParser
from pathlib import Path
import json
import unittest


class ScriptCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self._current_script = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._current_script = {"attrs": dict(attrs), "body": ""}

    def handle_data(self, data):
        if self._current_script is not None:
            self._current_script["body"] += data

    def handle_endtag(self, tag):
        if tag == "script" and self._current_script is not None:
            self.scripts.append(self._current_script)
            self._current_script = None


class WebAssetsTest(unittest.TestCase):
    def test_dashboard_has_no_esm_sh_executable_script(self):
        parser = ScriptCollector()
        parser.feed((Path(__file__).parent.parent / "web" / "index.html").read_text())

        executable_sources = [
            script["attrs"].get("src", "") + script["body"]
            for script in parser.scripts
        ]
        self.assertFalse(
            any("https://esm.sh/cuelume@0.1.2" in source for source in executable_sources),
            executable_sources,
        )


class TerminalControlSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.markup = (Path(__file__).parent.parent / "web" / "index.html").read_text()

    def test_obsolete_terminal_controls_are_absent(self):
        for control in (
            'id="fullscreenButton"',
            'id="terminalFullscreenButton"',
            'class="refresh-btn"',
            'class="history-btn"',
            'id="termHistory"',
        ):
            self.assertNotIn(control, self.markup, control)

    def test_browser_fullscreen_api_is_not_used(self):
        self.assertNotIn("requestFullscreen", self.markup)
        self.assertNotIn("exitFullscreen", self.markup)

    def test_the_composer_can_attach_a_file(self):
        """Attachments returned as a chunked upload, so the controls are back."""
        for control in ('id="attachmentInput"', 'id="attachmentPreview"', 'id="terminalUpload"'):
            self.assertIn(control, self.markup, control)
        # The single-message protocol is what failed; it must not come back.
        self.assertNotIn("send_attachment", self.markup)
        self.assertIn("attachment_begin", self.markup)
        self.assertIn("attachment_chunk", self.markup)


class CredentialFieldCollector(HTMLParser):
    """Collects the relay credential inputs and whether a form encloses them."""

    def __init__(self):
        super().__init__()
        self.inputs = {}
        self._form_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self._form_depth += 1
        elif tag == "input":
            attributes = dict(attrs)
            name = attributes.get("id")
            if name in {"relayUrl", "relayToken"}:
                self.inputs[name] = {
                    "attrs": attributes,
                    "in_form": self._form_depth > 0,
                }

    def handle_endtag(self, tag):
        if tag == "form" and self._form_depth > 0:
            self._form_depth -= 1


class CredentialAutofillTest(unittest.TestCase):
    """The browser never stores the token, so the platform password manager must.

    That only happens when the two fields sit in a form and carry the
    autocomplete roles managers look for.
    """

    def setUp(self):
        parser = CredentialFieldCollector()
        parser.feed((Path(__file__).parent.parent / "web" / "index.html").read_text())
        self.inputs = parser.inputs

    def test_relay_url_is_the_username_field(self):
        field = self.inputs["relayUrl"]
        self.assertTrue(field["in_form"], "relayUrl must sit inside a form")
        self.assertEqual(field["attrs"].get("autocomplete"), "username")
        self.assertTrue(field["attrs"].get("name"))

    def test_relay_token_is_the_password_field(self):
        field = self.inputs["relayToken"]
        self.assertTrue(field["in_form"], "relayToken must sit inside a form")
        self.assertEqual(field["attrs"].get("autocomplete"), "current-password")
        self.assertTrue(field["attrs"].get("name"))
        self.assertEqual(field["attrs"].get("type"), "password")


class SetupGuideLinkTest(unittest.TestCase):
    """The dashboard must send its own operator to its own documentation."""

    def test_setup_guide_points_at_this_fork(self):
        markup = (Path(__file__).parent.parent / "web" / "index.html").read_text()
        start = markup.index('id="relayCredentials"')
        hint = markup[start:markup.index("</div>", markup.index("hint", start))]

        self.assertIn("github.com/maxto/herdr-remote", hint)
        self.assertNotIn("dcolinmorgan", hint)

    def test_setup_guide_target_exists(self):
        guide = Path(__file__).parent.parent / "docs" / "TAILSCALE.md"
        self.assertTrue(guide.is_file(), "the linked guide must exist in the repo")


class HomeNavigationTest(unittest.TestCase):
    """The title is the way back, so it must be operable by keyboard too."""

    def test_the_title_is_a_button(self):
        markup = (Path(__file__).parent.parent / "web" / "index.html").read_text()
        header = markup[markup.index("<h1>"):markup.index("</h1>") + 5]

        self.assertIn("<button", header)
        self.assertIn('onclick="goHome()"', header)
        self.assertIn("aria-label", header)

    def test_going_home_clears_every_filter(self):
        source = (Path(__file__).parent.parent / "web" / "index.html").read_text()
        body = source[source.index("function goHome()"):source.index("function closeTerminal()")]

        for state in ("activePane", "activeSession", "activeWorkspace", "activeTab"):
            self.assertIn(state, body, state)


class ConnectButtonTest(unittest.TestCase):
    """The button is the only control that reports whether settings took."""

    def setUp(self):
        self.source = (Path(__file__).parent.parent / "web" / "index.html").read_text()

    def test_the_button_is_addressable(self):
        self.assertIn('id="connectButton"', self.source)

    def test_the_button_reports_every_state(self):
        body = self.source[self.source.index("function updateConnectButton("):]
        body = body[:body.index("\nfor (const field")]

        for label in ("Apply", "Connected", "Reconnect", "Connecting"):
            self.assertIn(label, body, label)
        self.assertIn("disabled", body)

    def test_editing_a_field_reopens_the_button(self):
        """A press only matters when there is something unapplied to commit."""
        self.assertIn("settingsDirty = true", self.source)
        self.assertIn("settingsDirty = false", self.source)
        self.assertIn("addEventListener('input'", self.source)

    def test_status_is_readable_without_relying_on_colour(self):
        card = self.source[self.source.index("function agentCard("):]
        card = card[:card.index("\nfunction ")]

        self.assertNotIn("status-pill", card)
        self.assertIn("${escAttr(a.status)}", card)


class StatusColourTest(unittest.TestCase):
    """The list and the log must never disagree about what a colour means."""

    def setUp(self):
        self.source = (Path(__file__).parent.parent / "web" / "index.html").read_text()

    def test_every_status_has_a_colour(self):
        table = self.source[self.source.index("const STATUS_COLORS"):]
        table = table[:table.index("};")]

        for status in ("blocked", "working", "done", "idle"):
            self.assertIn(status, table, status)

    def test_both_views_read_the_same_table(self):
        card = self.source[self.source.index("function agentCard("):]
        card = card[:card.index("\nfunction ")]
        timeline = self.source[self.source.index("function renderTimeline("):]
        timeline = timeline[:timeline.index("\nfunction ")]

        self.assertIn("statusColor(", card)
        self.assertIn("statusColor(", timeline)
        # A second, hand-rolled ternary is how the two drifted apart before.
        self.assertNotIn("'var(--red)'", card)
        self.assertNotIn("'var(--red)'", timeline)


class InstallabilityTest(unittest.TestCase):
    """Chrome offers to install a page only when its worker handles fetch."""

    def setUp(self):
        self.worker = (Path(__file__).parent.parent / "web" / "sw.js").read_text(encoding="utf-8")

    def test_the_service_worker_handles_fetch(self):
        # Without this Chrome offers "Add to Home screen", a shortcut, and
        # never the standalone install the manifest is written for.
        self.assertIn("addEventListener('fetch'", self.worker)

    def test_the_dashboard_is_never_served_from_a_cache(self):
        """A stale page speaks a stale protocol to a relay that has moved on."""
        for cached in ("caches.open", "cache.put", "cache.addAll", "caches.match"):
            self.assertNotIn(cached, self.worker, cached)


class NotificationVisibilityTest(unittest.TestCase):
    """Every push must leave something on screen.

    The subscription is userVisibleOnly, which Chrome enforces. The worker
    used to return early on a "clear" payload to close a notification without
    showing one, and Chrome filled the silence with "Il sito e stato
    aggiornato in background" — a placeholder that replaced the real news.
    """

    def setUp(self):
        self.worker = (Path(__file__).parent.parent / "web" / "sw.js").read_text(encoding="utf-8")

    def test_the_push_handler_always_shows_a_notification(self):
        handler = self.worker[self.worker.index("addEventListener('push'"):]
        handler = handler[:handler.index("addEventListener('notificationclick'")]

        self.assertIn("showNotification", handler)
        self.assertNotIn("return;", handler)
        self.assertNotIn("'clear'", handler)


class StaleNotificationTest(unittest.TestCase):
    """Once clearing stops travelling as a push, the page has to do it."""

    def setUp(self):
        self.source = (Path(__file__).parent.parent / "web" / "index.html").read_text()

    def test_a_snapshot_closes_what_it_no_longer_describes(self):
        body = self.source[self.source.index("function closeStaleNotifications("):]
        body = body[:body.index("\nfunction ")]

        self.assertIn("getNotifications", body)
        for tag in ("herdr-blocked", "herdr-done"):
            self.assertIn(tag, body, tag)


class ReconnectionTest(unittest.TestCase):
    """Coming back to the app must not be something to sit through.

    Android freezes a backgrounded page and the socket dies with it, so every
    return is a reconnection. The app waited out a flat three-second timer and
    announced "connecting" for each one, which turned a gap nobody would have
    noticed into a wait with a progress label on it.
    """

    def setUp(self):
        self.source = (Path(__file__).parent.parent / "web" / "index.html").read_text()

    def test_returning_to_the_app_reconnects_at_once(self):
        self.assertIn("visibilitychange", self.source)
        body = self.source[self.source.index("function reconnectNow()"):]
        body = body[:body.index("\nfunction ")]

        self.assertIn("connect()", body)
        # A refused token stops the retry loop on purpose: trying again only
        # replays the same credentials the relay already turned down.
        self.assertIn("unauthorized", body)

    def test_a_gap_shorter_than_a_glance_is_not_announced(self):
        body = self.source[self.source.index("function setStatus("):]
        body = body[:body.index("\nfunction paintStatus(")]

        self.assertIn("agents.length", body)
        self.assertIn("CONNECTING_GRACE_MS", body)

    def test_opening_the_app_opens_exactly_one_socket(self):
        """Two entry points used to race on every load.

        One opened a socket as the script ended and the other replaced it a
        tenth of a second later, so every cold start connected, dropped and
        reconnected — the flash of "connecting" that greeted every launch, and
        the pairs of one-second clients in the relay's log.
        """
        starts = [line.strip() for line in self.source.splitlines()
                  if "savedUrl" in line and "connect" in line]

        self.assertEqual(len(starts), 1, starts)

    def test_the_first_retry_does_not_wait_out_a_timer(self):
        line = self.source[self.source.index("const RECONNECT_LADDER"):]
        line = line[:line.index("\n")]
        first = int(line[line.index("[") + 1:line.index(",")])

        self.assertLess(first, 1000, line)


class ProductNameTest(unittest.TestCase):
    """The app answers to one name, written two ways for two audiences.

    Where the name is a label a person reads — under the icon on a home screen,
    and on the screen that introduces the app — it is set as a title, "Herdr
    App". Everywhere it is an identifier the operator reads next to code and
    hostnames, it stays "herdr-app". What must never happen again is the third
    and fourth spelling: the tab, the header and the welcome screen once said
    three different things between them.
    """

    LABEL = "Herdr App"
    HANDLE = "herdr-app"

    def setUp(self):
        self.web = Path(__file__).parent.parent / "web"
        self.source = (self.web / "index.html").read_text()

    def test_the_installed_app_is_labelled(self):
        manifest = json.loads((self.web / "manifest.webmanifest").read_text())

        self.assertEqual(manifest["name"], self.LABEL)
        self.assertEqual(manifest["short_name"], self.LABEL)

    def test_the_welcome_screen_introduces_the_app_by_label(self):
        self.assertIn(f"<strong>{self.LABEL}</strong>", self.source)

    def test_the_tab_and_the_header_use_the_handle(self):
        self.assertIn(f"<title>{self.HANDLE}</title>", self.source)
        self.assertIn(f">{self.HANDLE}</button></h1>", self.source)

    def test_a_notification_without_a_title_still_names_the_app(self):
        worker = (self.web / "sw.js").read_text(encoding="utf-8")

        self.assertIn(self.HANDLE, worker)

    def test_the_superseded_names_are_gone(self):
        for stale in ("Herdr Remote", "<title>herdr-remote</title>", ">herdr</button>"):
            self.assertNotIn(stale, self.source, stale)
