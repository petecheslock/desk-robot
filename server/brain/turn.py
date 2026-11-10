"""Turn-taking models: a real voice-activity detector and an end-of-turn judge.

Both are small ONNX models that run on the CPU in a few milliseconds.

* Silero VAD v6 (MIT): says whether a 32 ms chunk contains speech. It replaces
  the old "louder than X" test, but mic level and noise still affect detection.
* Smart Turn v3.2 (BSD-2, from the Pipecat project): listens to the whole
  current turn and says whether the person sounds finished or merely paused.
  It only runs when the VAD sees a pause, so it costs nothing while you talk.

The model files (2 MB + 9 MB) download once into ~/.cache/desk-robot/.
"""

from __future__ import annotations

import hashlib
import os
import ssl
import time
import urllib.request
from pathlib import Path

import certifi
import numpy as np

SAMPLE_RATE = 16_000
VAD_CHUNK = 512          # samples per VAD step (32 ms) — fixed by the Silero model
_VAD_CONTEXT = 64        # samples of the previous chunk the model wants to see again

CACHE_DIR = Path(os.environ.get("DESK_ROBOT_CACHE", Path.home() / ".cache" / "desk-robot"))

# Both downloads are pinned to an exact upstream revision and checked against
# a SHA-256 before use, so a changed or tampered file can't be loaded into the
# ONNX runtime. To upgrade a model: change the revision in the URL, download
# it, verify it yourself, and update the hash here.
SILERO_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/867c2aa69264/src/silero_vad/data/silero_vad.onnx"
)
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SMART_TURN_URL = (
    "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/f766f81d3cfd/smart-turn-v3.2-cpu.onnx"
)
SMART_TURN_SHA256 = "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 16):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, name: str, sha256: str) -> Path:
    """Download a model file once into the cache directory and verify it.
    A cached file that doesn't match the hash is thrown away and re-fetched."""
    path = CACHE_DIR / name
    if path.is_file():
        if _sha256(path) == sha256:
            return path
        print(f"cached {name} doesn't match its expected hash; downloading again")
        path.unlink()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"downloading {name} into {CACHE_DIR} (one time)...")
    ctx = ssl.create_default_context(cafile=certifi.where())
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=120, context=ctx) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
    got = _sha256(tmp)
    if got != sha256:
        tmp.unlink()
        raise RuntimeError(
            f"{name} from {url} has SHA-256 {got}, expected {sha256}: refusing to use it"
        )
    tmp.rename(path)
    return path


def _session(path: Path, threads: int = 1):
    import onnxruntime as ort  # slow import, keep it lazy

    so = ort.SessionOptions()
    so.inter_op_num_threads = 1
    so.intra_op_num_threads = threads
    so.log_severity_level = 3  # errors only
    return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])


class SileroVAD:
    """Feed 512-sample float32 chunks in order; get a speech probability (0..1)."""

    def __init__(self) -> None:
        self.session = _session(_fetch(SILERO_URL, "silero_vad.onnx", SILERO_SHA256))
        self.reset()
        self(np.zeros(VAD_CHUNK, dtype=np.float32))  # warm up (first run is slow)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, _VAD_CONTEXT), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        if len(chunk) != VAD_CHUNK:
            raise ValueError(f"VAD wants {VAD_CHUNK} samples, got {len(chunk)}")
        x = np.concatenate([self._context, chunk.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self.session.run(
            None, {"input": x, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self._context = x[:, -_VAD_CONTEXT:]
        return float(out[0, 0])


class SmartTurn:
    """Given the audio of the current turn (16 kHz float32), how likely is it
    that the person is done talking? Looks at the last 8 seconds."""

    WINDOW = 8 * SAMPLE_RATE

    def __init__(self, threads: int = 2) -> None:
        self.session = _session(_fetch(SMART_TURN_URL, "smart-turn-v3.2-cpu.onnx", SMART_TURN_SHA256), threads)
        self.last_ms = 0.0
        self.complete_probability(np.zeros(SAMPLE_RATE, dtype=np.float32))  # warm up

    def complete_probability(self, audio: np.ndarray) -> float:
        from .whisper_features import compute_whisper_log_mel_features

        t0 = time.time()
        audio = np.asarray(audio, dtype=np.float32)
        if len(audio) > self.WINDOW:
            audio = audio[-self.WINDOW:]
        elif len(audio) < self.WINDOW:
            audio = np.pad(audio, (self.WINDOW - len(audio), 0))  # zeros go at the front
        feats = compute_whisper_log_mel_features(audio, do_normalize=True)
        out = self.session.run(None, {"input_features": feats[np.newaxis]})
        self.last_ms = (time.time() - t0) * 1000
        return float(out[0][0, 0])
