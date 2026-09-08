from html.parser import HTMLParser
from pathlib import Path
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
