"""The console is ungated (fork simplification): the page, the live view and the status answer
anyone who can reach the port — the tunnel/port mapping is the boundary now. The one belt that
stays is the X-Rocky-Console header on POSTs, which a cross-site page can't send without a CORS
preflight we never grant. No network beyond localhost.
"""

import time
import unittest
import urllib.error
import urllib.request

from brain import eyes


def _get(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url: str, body: bytes, headers: dict | None = None):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ConsoleOpenTests(unittest.TestCase):
    """eyes.serve() on a scratch port, exercised like a browser would."""

    PORT = 18767

    @classmethod
    def setUpClass(cls):
        cls.eyes = eyes.Eyes()
        cls.eyes.serve(cls.PORT, "127.0.0.1")
        for _ in range(50):   # the server thread takes a moment
            try:
                _get(f"http://127.0.0.1:{cls.PORT}/status")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.05)

    def base(self):
        return f"http://127.0.0.1:{self.PORT}"

    def test_the_page_answers_with_no_token(self):
        status, body = _get(f"{self.base()}/")
        self.assertEqual(status, 200)
        self.assertIn(b"dashboard", body)
        self.assertNotIn(b"X-Rocky-Console-Token", body)

    def test_the_live_parts_answer_with_no_token(self):
        status, body = _get(f"{self.base()}/status")
        self.assertEqual(status, 200)
        self.assertIn(b"fps", body)

    def test_plain_cross_site_posts_are_still_refused(self):
        # a browser allows simple cross-site POSTs without a custom header: those must not reach controls
        status, _ = _post(f"{self.base()}/api/emotion", b'{"name":"happy"}',
                          {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_posts_with_the_console_header_get_through(self):
        # the header marks the real console; here the control itself doesn't exist (no command handler),
        # which answers 404 — proof the request made it past the guard
        status, _ = _post(f"{self.base()}/api/emotion", b'{"name":"happy"}',
                          {"Content-Type": "application/json", "X-Rocky-Console": "1"})
        self.assertEqual(status, 404)

    def test_serves_even_on_an_open_bind(self):
        e = eyes.Eyes()
        e.serve(18768, "0.0.0.0")
        status, _ = _get("http://127.0.0.1:18768/status")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
