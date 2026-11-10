"""The console token gate: one shared CONSOLE_TOKEN answers the dashboard, the live view and the
controls, whether the console comes from the brain's own server (eyes.py) or the launcher's proxy.
No network beyond localhost.
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from brain import config, eyes


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ConsoleTokenTests(unittest.TestCase):
    """eyes.serve() on a scratch port, exercised like a browser would."""

    TOKEN = "token-token-token-1234"
    PORT = 18767

    @classmethod
    def setUpClass(cls):
        cls.patch = patch.object(config, "CONSOLE_TOKEN", cls.TOKEN)
        cls.patch.start()
        cls.eyes = eyes.Eyes()
        cls.eyes.serve(cls.PORT, "127.0.0.1")   # loopback bind, but a token set: the token still gates
        for _ in range(50):   # the server thread takes a moment
            try:
                _get(f"http://127.0.0.1:{cls.PORT}/status", {"X-Rocky-Console-Token": cls.TOKEN})
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()

    def base(self):
        return f"http://127.0.0.1:{self.PORT}"

    def test_the_page_opens_with_the_token_in_the_url(self):
        status, body = _get(f"{self.base()}/?token={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn(b"dashboard", body)
        self.assertIn(self.TOKEN.encode(), body)   # handed to the page, kept in memory after this

    def test_without_a_token_nothing_answers(self):
        for path in ("/", "/status", "/frame", "/stream"):
            with self.subTest(path=path):
                status, _ = _get(self.base() + path)
                self.assertEqual(status, 403, path)

    def test_the_wrong_token_is_refused(self):
        status, _ = _get(f"{self.base()}/?token=not-the-token-at-all")
        self.assertEqual(status, 403)

    def test_fetches_carry_the_token_as_a_header(self):
        status, body = _get(f"{self.base()}/status", {"X-Rocky-Console-Token": self.TOKEN})
        self.assertEqual(status, 200)
        self.assertIn(b"fps", body)

    def test_controls_also_need_the_console_header(self):
        # even with the right token, a plain cross-site POST can't carry X-Rocky-Console
        req = urllib.request.Request(
            f"{self.base()}/api/emotion", data=b'{"name":"happy"}', method="POST",
            headers={"Content-Type": "application/json", "X-Rocky-Console-Token": self.TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                status = r.status
        except urllib.error.HTTPError as e:
            status = e.code
        self.assertEqual(status, 403)


class NoTokenLoopbackTests(unittest.TestCase):
    """No token configured and a loopback bind: local development, everything answers."""

    def test_serving_is_refused_without_a_token_on_a_open_bind(self):
        e = eyes.Eyes()
        with patch.object(config, "CONSOLE_TOKEN", ""), patch("builtins.print") as out:
            e.serve(18768, "0.0.0.0")
        self.assertIn("CONSOLE_TOKEN", out.call_args[0][0])

    def test_a_short_token_is_refused(self):
        e = eyes.Eyes()
        with patch.object(config, "CONSOLE_TOKEN", "short"), patch("builtins.print") as out:
            e.serve(18768, "0.0.0.0")
        self.assertIn("CONSOLE_TOKEN", out.call_args[0][0])

    def test_a_well_formed_token_on_an_open_bind_serves(self):
        e = eyes.Eyes()
        with patch.object(config, "CONSOLE_TOKEN", "token-token-token-1234"):
            e.serve(18768, "0.0.0.0")
        status, _ = _get("http://127.0.0.1:18768/status", {"X-Rocky-Console-Token": "token-token-token-1234"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
