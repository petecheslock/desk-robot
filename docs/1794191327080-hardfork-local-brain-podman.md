# Hard fork: local-brain server in podman (Linux), Fish Audio removed, browser A/V for testing

## Goal

Reposition the project: the brain server runs headless on a remote Linux box inside rootless podman. The ESP32 robot connects to it directly over the LAN WebSocket (protocol unchanged). The human tests and talks to it from the dashboard page opened via a Tailscale/ssh tunnel, using **that browser's** mic/webcam/speaker as a robot substitute while the real robot is offline. Future robot actions go through APIs/MCP, so all "control the host computer" code is deleted.

## Decisions already made (do not re-litigate)

1. **Fish Audio removed entirely** — it is hyper-specific to their models. Keep the generic OpenAI-audio speech server path (`TTS_SERVER_URL`/`TTS_SERVER_VOICE`, e.g. gufo + Qwen3-TTS) and the OS built-in fallback voice (espeak-ng on Linux). LLM endpoints stay (provider-agnostic via `LLM_BASE_URL`).
2. **Access model**: tailnet/ssh tunnel only. Keep it simple: robot WS keeps `ROBOT_TOKEN`; dashboard/live-view get **one shared `CONSOLE_TOKEN`** in place of the loopback/`Host: localhost` guards; bind on `0.0.0.0` inside the container; no TLS ceremony (tunnel provides privacy; `getUserMedia` works because `127.0.0.1` via tunnel is a secure context).
3. **`computer.py` deleted whole** (apps, Spotify/Music, volume, Shortcuts, Spotify keys) — future actions come via APIs/MCP servers, not host control.
4. **Host-device A/V deleted** (sounddevice mic, cv2 webcam, `say`/`afplay`/playback, `_play_mac`). When you want mic/camera/speaker without the robot, it comes from the **browser viewing the dashboard**.
5. **Browser A/V scope: robot-offline emulator only.** Browser mic drives wake/VAD/STT; browser plays replies. When the real robot connects, the server tells the browser to release its devices and the robot's A/V take over. No duplex browser audio while the robot is connected.
6. **Focus mode + Decisions API stay** (it's just another swappable endpoint; user will localize later).
7. **Ship as Quadlet + rootless podman** with systemd user service.
8. V1 freeze, upstream repo conventions, LICENSE, and firmware protocol stay untouched except where noted (firmware: comments/examples only).

## Current-state facts the plan relies on

- Robot link: firmware `BRAIN_HOST`/`BRAIN_PORT`/`ROBOT_TOKEN` (`firmware/include/secrets.h.example`) → `websockets.serve(handle_robot, "0.0.0.0", 8765)` (main.py:1740). No firmware change needed for remote hosting; just set `BRAIN_HOST` to the remote box's LAN IP.
- Browser-facing HTTP lives on the **brain's** `eyes.serve()` (port 8767): dashboard page, `/status`, `/frame`, `/stream`, `/api/*`. Launcher (8766) is a login-item wrapper around it with settings CRUD while stopped.
- Guards to replace: `launcher.py` `_local()`/`_console()` (LOCAL_HOSTS + `X-Rocky-Console`) and `eyes.py` `_local()`/`_from_this_computer()` (loopback-only POSTs).
- Audio wire format is already browser-compatible: 16 kHz mono s16le PCM (`docs/protocol.md`), robot mic uplink = binary `\x01`+PCM (`main.py:282`), speech downlink = `\x01`+PCM (`main.py:1001`).
- Wake/VAD/STT (`ears.py` + `turn.py` + faster-whisper) already consume pushed PCM (`ears.push_audio`), so a browser mic can reuse the entire pipeline with only source bookkeeping.

## Phase 1 — Strip Fish Audio

- `brain/mouth.py`: delete `FISH_TTS_URL`, `fish_available()`, `_fish_stream`; fallback chain becomes speech server → built-in voice. Re-point `Leveler.equalizer_key()` (drop the Fish `voice_id/model` key branch or key it off the server only). Update module docstring.
- `brain/config.py`: delete `TTS_VOICE_ID`, `TTS_MODEL`, `TTS_ACTING`; keep `TTS_SERVER_URL/VOICE`, `TTS_TEMPERATURE/TOP_P` (documented as generic sampling params), EQ/level knobs. Rewrite the comment blocks.
- `brain/characters.py`: replace the Fish voice model concept with a generic `voice` string (the `TTS_SERVER_VOICE` name the character speaks with, free-form ≤120 chars) or drop per-character voice if simpler — **keep the JSON schema version tolerant of existing files** (unknown keys ignored, missing `voice` = default). Delete `voice_sample()`, `TTS_MODELS`, `ACTING_MODELS`, 32-hex validation; delete the `_ACTING` block from the draft prompt (cues like `[small laugh]` no longer performed).
- `launcher.py`: delete `/launcher/characters/voice` route and `FISH_AUDIO_API_KEY` reads.
- `dashboard.html`: replace voice-ID/model fields + Fish hints/cost text with a plain "voice name on your speech server" field; remove "Test voice" proxy or re-point it at the configured speech server.
- `.env.example` / `requirements.txt`: drop `FISH_AUDIO_API_KEY`; keep `certifi` (still used for HTTPS to LLM/Spotify-free APIs — verify before dropping).
- Tests: `test_voice_server.py` — delete Fish fallback tests, keep server→builtin chain; `test_characters.py` — new voice-field expectations.
- `README.md` + `CHANGELOG.md`: rewrite the voice sections; add fork changelog entry.

## Phase 2 — Delete host/computer-specific code

- Delete `brain/computer.py`, `tests/test_computer.py`, `docs/computer-control-windows.md`.
- `main.py` + `thinking.py`: remove ACTIONS/QUICK tool wiring for computer control (`use_computer` switch-off tool gone too); remove `_play_mac`, `MAC_PLAY_LAG`, computer-speaker branches of `SPEAKER_OUT`, `MIC_SOURCE == "mac"` handling, `pick_camera_source`/`LocalCamera` usage.
- `ears.py`: keep `Ears` (robot-mic source, VAD feed); delete sounddevice device opening/names; `mic_device` settings die.
- `eyes.py`: delete `LocalCamera` + platform capture-backend map; keep `Eyes` frame store + `serve()` + `rotate_jpeg`.
- `mouth.py`: delete `_synthesize_builtin` darwin/win32 branches? **No** — keep espeak-ng path only (Linux fallback); drop `say`, PowerShell, `afplay`; `play()` deletes entirely (replaced by browser player in Phase 4).
- `config.py`: remove `TTS_FALLBACK_VOICE` (or keep as espeak voice name), `MIC_DEVICE`, `MIC_SOURCE`, `CAMERA_SOURCE`/`CAMERA_LOCAL_*`; update `.env.example`.
- `settings.py`: `mic` choice → `("auto","robot")`; `speaker_out` → robot-only (or drop); `camera` → `("robot","browser")` (Phase 4); delete `mic_device`.
- `launcher.py`: `/launcher/mics`, `/launcher/shortcuts`, `MIC_LIST`, `"mac"` flag in settings payload.
- `install_launcher.sh` → replace with Linux `systemd` user unit (Phase 5); delete launchd plist logic.
- Rename remaining "Mac"/"this computer" wording in code, logs, and dashboard to "browser"/"your machine".

## Phase 3 — Remote serving + simple token auth (tunnel-only threat model)

- New env `CONSOLE_TOKEN` (≥16 chars, same refusal style as `ROBOT_TOKEN`): required unless `LIVE_VIEW_BIND` is loopback-only, so local dev keeps working.
- `eyes.serve()`: drop `_local()` Host allowlist + `_from_this_computer()`; instead accept requests whose `Host` header matches configured `PUBLIC_HOST` (or any host when unset) **and** carry the token — GET via `?token=` (set as a JS var / in-memory on page load), POST via `X-Rocky-Console-Token`. `/stream` carries it in the URL. Keep the `X-Rocky-Console` custom-header requirement (cheap CSRF belt).
- `launcher.py`: same token gate replacing LOCAL_HOSTS checks; proxy to brain on `127.0.0.1:8767` inside the container unchanged.
- `config.LIVE_VIEW_BIND` default → `0.0.0.0` under the fork (container NAT handles exposure; tailnet is the only route).
- Docs: `.env.example` gains `CONSOLE_TOKEN`; README documents the tunnel (`tailscale serve` or `ssh -L 8766:localhost:8766 -L 8767:localhost:8767 <host>`; open `http://localhost:8766/`).

## Phase 4 — Browser A/V (robot-offline emulator)

Design (reuses robot wire semantics):

- Browser connects a second WebSocket to the brain's existing port 8765 with a **console hello** frame: `{"type":"hello_console","token":<CONSOLE_TOKEN>}` — validated like the robot hello; refused if a browser is already connected; refused outright while the robot socket is live.
- Uplink: browser captures via `getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}})` → `AudioWorklet` downsampled to 16 kHz s16le → binary `\x01`+PCM frames (≤100 ms), identical to the robot's mic path → `ears.push_audio()`.
- Downlink: server sends `\x01`+PCM speech chunks (same framing as robot speech) → browser `AudioWorklet`/scheduled `AudioBufferSourceNode` jitter buffer (~200 ms) → speaker. Reuse the reply pipeline (`speak`) with the new sink selected exactly where `_play_robot` vs `_play_mac` was chosen.
- Handover: on robot `hello`, server sends `{"type":"av_taken"}` → browser stops tracks (frees mic/camera) → normal robot flow. On robot disconnect, dashboard shows "Use this browser as mic/camera" toggle (user gesture resumes capture; no autoplay issues because replies play only after user interaction has already unlocked audio, and we surface a "click to enable audio" prompt otherwise).
- Browser webcam: while browser-A/V active, POST JPEG frames captured at ~5 fps via canvas to `POST /api/browser_frame` (token-gated) → `eyes.push_frame()`; respects `SEND_CAMERA_TO_BRAIN`.
- `settings`: `mic` → `("auto","robot","browser")`, `camera` → `("robot","browser")`; dashboard shows the current A/V source + start/stop controls and status on `/status` (browser mic fps, device label).
- `main.py`: source selection lives in `pick_mic_source`/`pick_camera_source` (browser replaces the deleted "mac" branch); sleep/awake, VAD, STT, snapshot paths unchanged (snapshot while browser-only: use latest frame).

## Phase 5 — Linux container packaging

- `deploy/Containerfile`: `python:3.12-slim`, install `espeak-ng` + `libgl1`/`libglib2.0-0` (opencv), `pip install -r server/requirements.txt`, copy `server/`, `USER 1000`, `ENV HOME=/home/rocky` (model cache under a named volume), entrypoint `python launcher.py` with `ROCKY_START_BRAIN=1`.
- `deploy/desk-robot.container` (Quadlet): rootless, `PublishPort=8765` (LAN for robot) and `PublishPort=127.0.0.1:8766:8766` + `127.0.0.1:8767:8767` (tunnel-only console), `EnvironmentFile=%h/.config/desk-robot/.env`, `Volume=desk-robot-characters:/srv/desk-robot/server/characters`, `Volume=desk-robot-cache:/home/rocky/.cache`, `Restart=on-failure`, `AddCapability=NET_BIND_SERVICE` not needed (ports >1024).
- Model weight strategy: faster-whisper base.en pinned commit (~145 MB) auto-downloads into the cache volume on first start (documented); note the `STT_REVISION` pin already covers supply-chain. Alternative documented: pre-bake weights into the image.
- Verification commands + README section: `podman build -t desk-robot .`, `podman generate systemd`-free — quadlet unit install to `~/.config/containers/systemd/`, `systemctl --user daemon-reload && systemctl --user start desk-robot`.
- Firmware doc: point `BRAIN_HOST` at the remote box's LAN IP; same `ROBOT_TOKEN`. No protocol changes.

## Phase 6 — Docs & repo hygiene

- README: rewrite "Set up the brain on your computer" → "Run the brain on your server"; remove Mac sections (mic picker, camera permission, launchd, Windows/Linux caveats); rewrite voice setup (speech server only); keep hardware/build/firmware chapters.
- `.env.example` final shape: `LLM_API_KEY/LLM_BASE_URL/LLM_MODEL`, `TTS_SERVER_URL/TTS_SERVER_VOICE`, `OPENAI_API_KEY` (focus), `HUMAN_NAME`, `ROBOT_TOKEN`, `CONSOLE_TOKEN`, `DESK_ROBOT_CACHE`.
- CHANGELOG: fork entry summarizing removals.

## Test/validation plan

- `python -m pytest server/tests` (unittest suite) after each phase — target: green with Fish/computer tests deleted, browser-A/V source-selection unit tests added (hello_console auth, av_taken handover, mic source state machine, token guard).
- Manual on the Linux box: start container → `/status` answers over tunnel → connect robot with `BRAIN_HOST` set → voice loop works → kill robot, start browser A/V in dashboard, say "hey <name>", get spoken reply in browser.
- Fallback chain check: no `TTS_SERVER_URL` → espeak-ng speaks in container.

## Risks / notes

- `mediapipe`+`opencv` on slim Debian: verify wheel installs for target arch (assume x86_64; ARM needs a check).
- Browser mic quality (echo cancellation, AGC) may shift `VAD_THRESHOLD` tuning vs the robot mic.
- Dropping the Host allowlist weakens DNS-rebinding defense; acceptable per tunnel-only decision — `CONSOLE_TOKEN` is the real gate.
- Existing `server/characters/*.json` and `settings.json` on the fork host must survive schema changes; write tolerant loading, no migration file needed.
- Launcher's stdin console stays (works via `podman exec -it` or `journalctl`), documented as the raw-console path.

## Out of scope

- TLS/HTTPS for the console, OIDC, multi-robot support.
- Duplex browser audio while robot connected.
- MCP/API action backends (future fork feature).
- Firmware protocol changes.
