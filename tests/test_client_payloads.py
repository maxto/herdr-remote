import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ClientPayloadTests(unittest.TestCase):
    def test_web_preserves_and_sends_omp_question_state(self):
        source = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        for field in (
            "prompt_id",
            "multi_options",
            "selected_options",
            "interaction",
            "multi",
        ):
            self.assertIn(field, source)
        self.assertIn("type:'question_toggle'", source)
        self.assertIn("type:'question_submit'", source)
        self.assertIn("prompt_id:a.prompt_id", source)

    def test_web_shows_the_owning_session_and_never_rebuilds_pane_ids(self):
        source = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        # The session comes from the relay as an explicit field, never inferred
        # from cwd, terminal title, or the shape of the pane id.
        self.assertIn("session_name", source)
        self.assertIn("session-badge", source)
        self.assertIn("sessionBadge(a)", source)
        # Grouping is keyed on the namespaced workspace id, so same-named
        # workspaces from different sessions cannot merge.
        self.assertIn("activeSession", source)
        self.assertIn("a.session_name === activeSession", source)
        # Actions send back the relay's identifier verbatim.
        self.assertIn("openTerminal(this.dataset.paneId)", source)
        self.assertNotIn("`${a.session_name}:${a.herdr_pane_id}`", source)

    def test_swift_models_decode_omp_question_state(self):
        for relative_path in (
            "herdi-ios/Sources/Models/Agent.swift",
            "herdi-mac/Sources/Agent.swift",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            for field in (
                "prompt_id",
                "multi_options",
                "selected_options",
                "interaction",
                "multi",
            ):
                self.assertIn(field, source)
            self.assertIn('let type = "question_toggle"', source)
            self.assertIn('let type = "question_submit"', source)

    def test_native_clients_render_and_send_multi_selection_state(self):
        ios = (ROOT / "herdi-ios" / "Sources" / "Views" / "ApprovalView.swift").read_text(
            encoding="utf-8"
        )
        mac = (ROOT / "herdi-mac" / "Sources" / "NotchContentView.swift").read_text(
            encoding="utf-8"
        )

        for source in (ios, mac):
            self.assertIn("selectedOptions.contains(option)", source)
            self.assertIn("toggleQuestionOption", source)
            self.assertIn("submitQuestion", source)

    def test_native_question_actions_are_connection_methods(self):
        for relative_path in (
            "herdi-ios/Sources/Services/RelayConnection.swift",
            "herdi-mac/Sources/RelayConnection.swift",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            for method in ("toggleQuestionOption", "submitQuestion"):
                declaration = next(
                    line for line in source.splitlines() if f"func {method}(" in line
                )
                self.assertTrue(
                    declaration.startswith("    func "),
                    f"{method} must be declared at RelayConnection class scope",
                )

    def test_tui_sends_multi_selection_messages_with_prompt_identity(self):
        source = (ROOT / "relay" / "herdr_tui.py").read_text(encoding="utf-8")

        self.assertIn('"type": "question_toggle"', source)
        self.assertIn('"type": "question_submit"', source)
        self.assertIn('"prompt_id": event.prompt_id', source)
        self.assertIn("selected_options", source)


if __name__ == "__main__":
    unittest.main()
