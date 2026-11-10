"""The browser standing in for the robot while it's offline: the console WebSocket's hello and
token, the handover when the real robot connects, and which mic he listens through. Real
WebSockets on a scratch port, so the framing is exercised too."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

import websockets

from brain import config, main, settings


def sample_pcm(ms: float = 100.0) -> bytes:
    import numpy as np
    return (np.random.default_rng(0).uniform(-0.2, 0.2, int(16000 * ms / 1000)) * 32767).astype("<i2").tobytes()


class FakeEars:
    def __init__(self):
        self.source = "robot"
        self.muted = asyncio.Event()
        self.muted.clear()
        self.pushed: list[tuple[bytes, str]] = []

    def set_source(self, source):
        self.source = source

    def push_audio(self, pcm16, source="robot"):
        self.pushed.append((pcm16, source))

    def apply_config(self):
        pass


class BrowserAVTests(unittest.TestCase):
    TOKEN = "console-token-console-token"
    ROBOT_TOKEN = "robot-token-robot-token"

    def setUp(self):
        self.saved = (config.CONSOLE_TOKEN, main.robot_socket, main.browser_socket, main.browser_devices,
                      config.MIC_SOURCE, main.ears, main.current_settings)
        self.prints = patch("builtins.print")
        self.prints.start()

    def tearDown(self):
        self.prints.stop()
        (config.CONSOLE_TOKEN, main.robot_socket, main.browser_socket, main.browser_devices,
         config.MIC_SOURCE, main.ears, main.current_settings) = self.saved
        main.browser_mic_times.clear()

    def run_async(self, coro):
        return asyncio.run(coro)

    # ── the hello and its token ──────────────────────────────────────────────
    async def _serve(self):
        server = await websockets.serve(main.handle_socket, "127.0.0.1", 0, max_size=1024 * 1024)
        port = server.sockets[0].getsockname()[1]
        return server, port

    async def _hello_console(self, port, token=TOKEN, extra=None):
        """Connect, say hello_console, return (websocket_or_None, first_message_or_close_reason)."""
        ws = await websockets.connect(f"ws://127.0.0.1:{port}", origin="http://localhost:8766")
        hello = {"type": "hello_console", "token": token, **(extra or {})}
        try:
            await ws.send(json.dumps(hello))
            first = await asyncio.wait_for(ws.recv(), timeout=3)
            return ws, first
        except websockets.ConnectionClosed as e:
            return None, e.rcvd.code if e.rcvd else None

    def test_a_good_hello_is_welcomed(self):
        async def go():
            server, port = await self._serve()
            try:
                with patch.object(config, "CONSOLE_TOKEN", self.TOKEN):
                    ws, first = await self._hello_console(port, extra={"mic": "MacBook mic", "camera": "FaceTime"})
                    self.assertIsNotNone(ws)
                    self.assertEqual(json.loads(first)["type"], "console_ok")
                    self.assertEqual(main.browser_devices["mic"], "MacBook mic")
                    self.assertEqual(main.browser_devices["camera"], "FaceTime")
                    await ws.close()
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    def test_a_bad_or_missing_token_is_refused(self):
        async def go():
            server, port = await self._serve()
            try:
                with patch.object(config, "CONSOLE_TOKEN", self.TOKEN):
                    for bad in ("", "wrong-token-wrong-token", self.ROBOT_TOKEN):
                        with self.subTest(token=bad):
                            ws, code = await self._hello_console(port, bad)
                            self.assertIsNone(ws)
                            self.assertEqual(code, 1008)
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    def test_a_second_browser_is_refused_while_one_stands_in(self):
        async def go():
            server, port = await self._serve()
            try:
                with patch.object(config, "CONSOLE_TOKEN", self.TOKEN):
                    first, _ = await self._hello_console(port)
                    self.assertIsNotNone(first)
                    second, code = await self._hello_console(port)
                    self.assertIsNone(second)
                    self.assertEqual(code, 1013)
                    await first.close()
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    def test_the_robot_port_refuses_posing_as_the_robot_from_a_browser(self):
        async def go():
            server, port = await self._serve()
            try:
                with patch.dict(os.environ, {"ROBOT_TOKEN": self.ROBOT_TOKEN}):
                    ws = await websockets.connect(f"ws://127.0.0.1:{port}", origin="http://evil.example")
                    await ws.send(json.dumps({"type": "hello", "token": self.ROBOT_TOKEN}))
                    with self.assertRaises(websockets.ConnectionClosed) as closed:
                        await ws.recv()
                    self.assertEqual(closed.exception.rcvd.code, 1008)
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    # ── the handover when the real robot connects ───────────────────────────
    def test_the_robot_takes_over_and_the_browser_lets_go(self):
        async def go():
            server, port = await self._serve()
            main.ears = FakeEars()   # so the mic source has somewhere to land
            try:
                with patch.object(config, "CONSOLE_TOKEN", self.TOKEN), \
                        patch.dict(os.environ, {"ROBOT_TOKEN": self.ROBOT_TOKEN}):
                    browser, first = await self._hello_console(port)
                    self.assertIsNotNone(browser)
                    # the robot connects with a proper hello (a browser origin, unlike the robot's)
                    robot = await websockets.connect(f"ws://127.0.0.1:{port}", origin="file://")
                    await robot.send(json.dumps({"type": "hello", "token": self.ROBOT_TOKEN, "fw": "0.4.4"}))
                    # the browser is told to let go, and keeps its connection for when the robot leaves
                    msg = await asyncio.wait_for(browser.recv(), timeout=3)
                    self.assertEqual(json.loads(msg)["type"], "av_taken")
                    self.assertIsNotNone(main.robot_socket)
                    # the robot's mic is live now, not the browser's
                    self.assertEqual(self._mic_source(), "robot")
                    # a browser that tries to (re)connect while the robot is live is refused
                    again, code = await self._hello_console(port)
                    self.assertIsNone(again)
                    self.assertEqual(code, 1013)
                    # the robot leaves: the browser's session is still there, and its mic is back
                    await robot.close()
                    await asyncio.sleep(0.1)
                    self.assertIsNone(main.robot_socket)
                    self.assertEqual(self._mic_source(), "browser")
                    await browser.close()
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    def _mic_source(self):
        ears = main.ears
        return ears.source if ears is not None else "robot"

    # ── the mic uplink ───────────────────────────────────────────────────────
    def test_the_browsers_mic_feeds_the_ears(self):
        async def go():
            server, port = await self._serve()
            ears = FakeEars()
            main.ears = ears
            try:
                with patch.object(config, "CONSOLE_TOKEN", self.TOKEN):
                    ws, _ = await self._hello_console(port)
                    self.assertIsNotNone(ws)
                    self.assertEqual(ears.source, "browser")   # picked on connect
                    pcm = sample_pcm()
                    await ws.send(b"\x01" + pcm)
                    await asyncio.sleep(0.2)
                    self.assertEqual(ears.pushed, [(pcm, "browser")])
                    # a frame split mid-sample (even length), or not mic audio, is dropped whole
                    ears.pushed.clear()
                    await ws.send(b"\x01\x02")
                    await ws.send(b"\x02" + pcm)
                    await asyncio.sleep(0.2)
                    self.assertEqual(ears.pushed, [])
                    await ws.close()
            finally:
                server.close()
                await server.wait_closed()
        self.run_async(go())

    # ── the mic source state machine (no sockets needed) ─────────────────────
    def test_which_mic_he_listens_through(self):
        ears = FakeEars()
        main.ears = ears
        robot, browser = object(), object()
        for mode, robot_on, browser_on, want in (
                ("auto",   True,  True,  "robot"),    # the robot's A/V always wins
                ("auto",   True,  False, "robot"),
                ("auto",   False, True,  "browser"),  # the browser stands in
                ("auto",   False, False, "robot"),    # nothing to hear yet
                ("robot",  False, True,  "robot"),    # pinned to the robot's
                ("browser", False, True,  "browser"),
                ("browser", False, False, "robot")):
            with self.subTest(mode=mode, robot=robot_on, browser=browser_on):
                config.MIC_SOURCE = mode
                main.robot_socket = robot if robot_on else None
                main.browser_socket = browser if browser_on else None
                main.pick_mic_source()
                self.assertEqual(ears.source, want)

    def test_the_browser_camera_choice_gates_the_webcam(self):
        s = settings.defaults()
        self.assertEqual(s["vision"]["camera"], "browser")   # the fork's default: the dashboard stands in
        with self.assertRaises(ValueError):
            settings.clean_value("vision", "camera", "mac")  # no host camera any more
        for mic in ("auto", "robot", "browser"):
            self.assertEqual(settings.clean_value("rocky", "mic", mic), mic)

    def test_dashboard_mic_capture_is_chrome_worklet_only(self):
        # Chrome-only capture: mic -> AudioWorklet -> WebSocket. The Firefox workarounds
        # (WebRTC loopback, ScriptProcessor/Analyser fallbacks, split getUserMedia, keep-alive
        # oscillator) were removed — guard that the worklet path stays and the hacks don't creep back.
        from pathlib import Path
        page = (Path(__file__).resolve().parents[1] / "brain" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("avCaptureWorklet()", page)
        self.assertIn("new MediaStream(stream.getVideoTracks())", page)
        self.assertIn("createMediaStreamSource", page)
        for removed in ("avCaptureScript", "avCaptureAnalyser", "avLoopback", "RTCPeerConnection",
                        "getByteTimeDomainData", "iceGatheringState", "split getUserMedia",
                        "avKeepAlive", "probeBrowserMic"):
            self.assertNotIn(removed, page, f"Firefox hack {removed!r} should be gone")


if __name__ == "__main__":
    unittest.main()
