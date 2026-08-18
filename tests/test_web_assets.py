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
