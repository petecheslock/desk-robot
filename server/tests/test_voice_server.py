"""A speech server of your own instead of Fish Audio: what it's asked for, and the audio
that comes back at the model's own rate becoming the 16 kHz the robot's speaker expects.Covers the request that goes out, the rate that comes back, and falling through to
the next voice when the server isn't there. Every fake takes **kw because a request
made over https carries a certifi `context` that a stub would otherwise choke on.
"""

import io
import json
import unittest
import urllib.error
import urllib.parse
import wave
from unittest.mock import patch

import numpy as np

from brain import config, mouth


class FakeResponse:
    """The bit of a urllib response _server_stream reads."""

    def __init__(self, chunks, ctype="audio/pcm"):
        self.chunks = list(chunks)
        self.headers = {"Content-Type": ctype}

    def read(self, n=0):
        return self.chunks.pop(0) if self.chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_urlopen(fn):
    return patch.object(mouth.urllib.request, "urlopen", fn)


class AvailableTest(unittest.TestCase):
    def test_needs_a_url(self):
        with patch.object(config, "TTS_SERVER_URL", ""), patch.object(config, "TTS_SERVER_VOICE", "rocky"):
            self.assertFalse(mouth.voice_server_available())

    def test_a_url_without_a_voice_says_what_is_missing(self):
        with patch.object(config, "TTS_SERVER_URL", "http://host:8190"), \
                patch.object(config, "TTS_SERVER_VOICE", ""), \
                patch("builtins.print") as out:
            self.assertFalse(mouth.voice_server_available())
        self.assertIn("TTS_SERVER_VOICE", out.call_args[0][0])


class RequestTest(unittest.TestCase):
    def setUp(self):
        mouth._SERVER = None
        self.addCleanup(setattr, mouth, "_SERVER", None)
        self.patches = (patch.object(config, "TTS_SERVER_URL", "http://host:8190/"),
                        patch.object(config, "TTS_SERVER_VOICE", "rocky"),
                        patch.object(config, "TTS_TEMPERATURE", 0.4),
                        patch.object(config, "TTS_TOP_P", 0.6))
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _run(self, body=b"\0" * 9600):
        seen = []

        def fake(req, timeout=0, **kw):
            seen.append(req)
            if req.full_url.endswith("/v1/models"):
                return FakeResponse([json.dumps({"data": [{"id": "gufo-tts"}]}).encode()], "application/json")
            if json.loads(req.data)["input"] == ".":      # the startup rate probe
                return FakeResponse([b"\0" * 4800])
            return FakeResponse([body])

        with patch_urlopen(fake):
            list(mouth._server_stream("Hello there."))
        return [r for r in seen
                if r.full_url.endswith("/v1/audio/speech") and json.loads(r.data)["input"] != "."]

    def test_the_server_named_in_the_config_is_the_one_called(self):
        seen = self._run()
        urls = {r.full_url for r in seen}
        self.assertIn("http://host:8190/v1/audio/speech", urls)   # the trailing slash didn't double up
        self.assertNotIn("http://host:8190//v1/audio/speech", urls)

    def test_asks_for_pcm_and_names_the_model_the_server_reports(self):
        for r in self._run():
            body = json.loads(r.data)
            self.assertEqual(body["model"], "gufo-tts")    # from /v1/models, not guessed
            self.assertEqual(body["voice"], "rocky")
            self.assertEqual(body["input"], "Hello there.")
            self.assertEqual(body["response_format"], "pcm")   # a WAV would be buffered, so no streaming
            self.assertEqual(body["temperature"], 0.4)
            self.assertEqual(body["top_p"], 0.6)

    def test_no_key_is_sent(self):
        """These servers have no authentication: nothing that looks like a credential goes out."""
        for r in self._run():
            for name in r.header_items():
                self.assertNotIn(name[0].lower(), ("authorization", "x-api-key", "api-key"))

    def test_a_server_that_only_serves_wav_still_works(self):
        """Some servers ignore response_format and answer with a WAV anyway."""

        def wav(seconds, rate=24_000):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(np.zeros(int(rate * seconds), np.int16).tobytes())
            return buf.getvalue()

        def fake(req, timeout=0, **kw):
            if req.full_url.endswith("/v1/models"):
                return FakeResponse([json.dumps({"data": [{"id": "m"}]}).encode()], "application/json")
            # the startup probe asks for a WAV and reads its rate; so does the reply
            return FakeResponse([wav(0.01 if json.loads(req.data)["input"] == "." else 1.0)], "audio/wav")

        with patch_urlopen(fake):
            out = b"".join(mouth._server_stream("Hello."))
        self.assertEqual(len(out) // 2, 16_000)    # one second of 24 kHz became one second of 16


class RateTest(unittest.TestCase):
    def setUp(self):
        mouth._SERVER = None
        self.addCleanup(setattr, mouth, "_SERVER", None)

    def test_a_server_without_a_model_id_is_not_probed_for_rate(self):
        """A /v1/models that says nothing means the endpoint is not usable: the 30 s rate
        probe and the speech POST are skipped, so a dead address fails once, not per sentence."""
        seen = []

        def fake(req, timeout=0, **kw):
            seen.append(req.full_url)
            return FakeResponse([json.dumps({"data": []}).encode()], "application/json")

        with patch.object(config, "TTS_SERVER_URL", "http://host:8190"), \
                patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch_urlopen(fake), patch("builtins.print") as out:
            with self.assertRaises(RuntimeError):
                list(mouth._server_stream("Hello."))
        self.assertEqual(seen, ["http://host:8190/v1/models"])
        self.assertIn("no model id", out.call_args[0][0])

    def test_the_rate_comes_from_the_probe_not_from_a_guess(self):
        """A reply played back at the wrong rate is chipmunk-voiced, so the WAV header decides."""

        def wav_at(rate, seconds=1.0):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
                w.writeframes(np.zeros(int(rate * seconds), np.int16).tobytes())
            return buf.getvalue()

        for rate in (16_000, 22_050, 24_000, 48_000):
            mouth._SERVER = None
            with patch.object(config, "TTS_SERVER_URL", "http://host:1"), \
                    patch_urlopen(lambda req, timeout=0, **kw: FakeResponse(
                        [json.dumps({"data": [{"id": "m"}]}).encode()], "application/json")
                    if req.full_url.endswith("/v1/models") else FakeResponse([wav_at(rate)], "audio/wav")):
                self.assertEqual(mouth._server()["rate"], rate)

    def test_pcm_is_resampled_to_sixteen_k(self):
        mouth._SERVER = {"model": "m", "rate": 24_000}
        tone = (np.sin(2 * np.pi * 200 * np.arange(24_000) / 24_000) * 12_000).astype(np.int16)
        with patch.object(config, "TTS_SERVER_URL", "http://host:1"), \
                patch_urlopen(lambda req, timeout=0, **kw: FakeResponse([tone.tobytes()])):
            out = b"".join(mouth._server_stream("Hello."))
        self.assertEqual(len(out) // 2, 16_000)
        y = np.frombuffer(out, dtype=np.int16).astype(np.float32)
        f = np.fft.rfftfreq(len(y), 1 / 16_000)
        p = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        self.assertAlmostEqual(f[p.argmax()], 200, delta=5)   # the pitch survived, and so did the level

    def test_a_chunk_boundary_never_splits_a_sample(self):
        mouth._SERVER = {"model": "m", "rate": 24_000}
        odd = [b"\x01" * 4097, b"\x02" * 4095, b"\x03" * 7]   # 8199 bytes: an odd total on purpose
        with patch.object(config, "TTS_SERVER_URL", "http://host:1"), \
                patch_urlopen(lambda req, timeout=0, **kw: FakeResponse(odd)):
            chunks = list(mouth._server_stream("Hello."))
        for c in chunks[:-1]:
            self.assertEqual(len(c) % 2, 0)
        self.assertEqual(sum(len(c) for c in chunks) % 2, 0)


class PriorityTest(unittest.TestCase):
    """The server wins over Fish Audio, Fish over the computer's voice, and a dead one falls through."""

    def setUp(self):
        mouth._SERVER = {"model": "m", "rate": 24_000}
        self.addCleanup(setattr, mouth, "_SERVER", None)

    def _run(self, url, dead=(), fish=True):
        """Speak once against a server at `url` ("" = none), whose speech calls fail when its
        host is in `dead`, and record what was asked of whom. Fish answers a bad voice ID
        with 400 and nothing else, which is what makes it fall through here too."""
        calls = []

        def fake(req, timeout=0, **kw):
            calls.append(req.full_url)
            if req.full_url.endswith("/v1/models"):
                return FakeResponse([json.dumps({"data": [{"id": "m"}]}).encode()], "application/json")
            host = urllib.parse.urlparse(req.full_url).netloc.split(":")[0]
            if json.loads(req.data).get("input") == ".":      # the startup rate probe
                if host in dead:
                    raise urllib.error.URLError("nobody home")
                return FakeResponse([b"\0" * 48_000])         # 1 s at the server's own rate
            if host in dead:
                raise urllib.error.URLError("nobody home")
            if "fish.audio" in host:
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)
            return FakeResponse([b"\0" * 48_000])

        with patch.object(config, "TTS_SERVER_URL", url), patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch.object(config, "TTS_VOICE_ID", "0123456789abcdef0123456789abcdef"), \
                patch.dict("os.environ", {"FISH_AUDIO_API_KEY": "k" if fish else ""}), \
                patch.object(mouth, "_synthesize_builtin", lambda text: b"\7" * 512), \
                patch_urlopen(fake):
            audio = b"".join(mouth.stream("Hello there."))
        return calls, audio

    def speech_to(self, calls, host):
        """Speech requests sent to one host: everything but its /v1/models read
        (Fish's route is /v1/tts, so the path says nothing useful here)."""
        return [c for c in calls if host in c and not c.endswith("/v1/models")]

    def test_the_server_is_used_instead_of_fish_audio(self):
        calls, audio = self._run("http://host:8190")
        self.assertEqual(len(self.speech_to(calls, "host:8190")), 1)     # his line; Fish stays untouched
        self.assertFalse(self.speech_to(calls, "fish.audio"))
        self.assertGreater(len(audio), 0)

    def test_fish_audio_still_works_when_no_server_is_set(self):
        calls, audio = self._run("")
        self.assertEqual(len(self.speech_to(calls, "fish.audio")), 1)
        self.assertFalse(self.speech_to(calls, "host:8190"))

    def test_a_server_that_is_down_does_not_silence_the_robot(self):
        """One provider going down must never leave a reply unsaid: it falls through to
        Fish Audio, and from there to the computer's own voice. The name is .invalid,
        which no resolver can answer, so this needs no network."""
        dead = "nonexistent-host.invalid"
        calls, audio = self._run(f"http://{dead}:8190", dead={dead})
        self.assertTrue(any(dead in c for c in calls))
        self.assertEqual(len(self.speech_to(calls, "fish.audio")), 1)    # fell through to Fish
        self.assertGreater(len(audio), 0)


class EqualizerKeyTest(unittest.TestCase):
    def test_the_two_voices_do_not_share_what_the_equalizer_learned(self):
        with patch.object(config, "TTS_SERVER_URL", "http://host:8190"), \
                patch.object(config, "TTS_SERVER_VOICE", "rocky"):
            server = mouth.Leveler.equalizer_key()
        with patch.object(config, "TTS_SERVER_URL", ""):
            fish = mouth.Leveler.equalizer_key()
        self.assertNotEqual(server, fish)
        self.assertIn("rocky", server)

    def test_the_same_server_under_two_spellings_is_one_voice(self):
        with patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch.object(config, "TTS_SERVER_URL", "http://host"):
            bare = mouth.Leveler.equalizer_key()
        with patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch.object(config, "TTS_SERVER_URL", "http://host:80"):
            explicit = mouth.Leveler.equalizer_key()
        self.assertEqual(bare, explicit)
        with patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch.object(config, "TTS_SERVER_URL", "http://host:8190"):
            self.assertNotEqual(bare, mouth.Leveler.equalizer_key())   # a real port still tells them apart


class ActingCueTest(unittest.TestCase):
    """An acting character's [directions] and <whisper> tags are for Drama 3 to perform.
    A generic speech server reads them out, so when that's the voice, the cues must not reach it."""

    def _asked_text(self, url):
        asked = []

        def fake(req, timeout=0, **kw):
            if req.full_url.endswith("/v1/models"):
                return FakeResponse([json.dumps({"data": [{"id": "m"}]}).encode()], "application/json")
            body = json.loads(req.data)
            if body.get("input", body.get("text")) != ".":   # not the startup rate probe
                asked.append(body.get("input") or body.get("text"))
            return FakeResponse([b"\0" * 9600])

        with patch.object(config, "TTS_ACTING", True), \
                patch.object(config, "TTS_SERVER_URL", url), \
                patch.object(config, "TTS_SERVER_VOICE", "rocky"), \
                patch.object(config, "TTS_VOICE_ID", "0123456789abcdef0123456789abcdef"), \
                patch.dict("os.environ", {"FISH_AUDIO_API_KEY": "k"}), \
                patch.object(mouth, "_synthesize_builtin", lambda text: b""), \
                patch_urlopen(fake):
            mouth._SERVER = {"model": "m", "rate": 24_000}
            list(mouth.stream("[sighs] Long <whisper>day</whisper>."))
        return asked

    def test_the_server_is_asked_for_the_words_alone(self):
        texts = self._asked_text("http://host:8190")
        self.assertEqual(len(texts), 1)
        self.assertNotIn("[sighs]", texts[0])
        self.assertNotIn("<whisper>", texts[0])

    def test_fish_still_gets_the_cues_to_perform(self):
        texts = self._asked_text("")
        self.assertEqual(len(texts), 1)
        self.assertIn("[sighs]", texts[0])
        self.assertIn("<whisper> day </whisper>", texts[0])   # clean_for_acting keeps the cues, spaced

    def test_the_prompt_only_offers_cues_to_an_acting_voice(self):
        from brain import personality
        ch = {"name": "J", "prompt": "You are J.", "examples": "[happy] Hi.",
              "lines": {}, "wake_words": ["hey j"]}
        with patch.object(config, "TTS_ACTING", True), \
                patch.object(config, "TTS_SERVER_URL", "http://host:8190"), \
                patch.object(config, "TTS_SERVER_VOICE", "rocky"):
            self.assertNotIn("Your voice can act", personality._character_prompt(ch))
        with patch.object(config, "TTS_ACTING", True), \
                patch.object(config, "TTS_SERVER_URL", ""):
            self.assertIn("Your voice can act", personality._character_prompt(ch))


if __name__ == "__main__":
    unittest.main()
