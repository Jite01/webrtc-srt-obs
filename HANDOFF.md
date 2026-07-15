# Project Handoff

## Project Goal

Phase 1A is a single-phone, single-server, single-OBS low-latency contribution pipeline. A phone browser captures camera and microphone media, sends it to the Python server using WebRTC, and the server delivers it to OBS as MPEG-TS over SRT.

```text
Phone browser
  | WebRTC: SRTP audio/video + HTTP(S) signaling
  v
aiortc in server.py
  | decoded VideoFrame / AudioFrame
  v
FFmpeg: libx264 + AAC
  | MPEG-TS over SRT
  v
OBS Media Source in SRT listener mode
```

Target glass-to-glass latency is 400-800 ms on a stable LAN. RTMP and nginx are intentionally absent because their normal buffering prevents this target.

## Current Status

Implementation is complete for the Phase 1A scope. It supports exactly one active browser connection and creates one FFmpeg process per active contribution.

Validated during this session:

- FFmpeg 8.0.1 is installed and reports SRT, `libx264`, and AAC support.
- A project-local `.venv` was created with Python 3.14.4.
- `aiohttp 3.14.1`, `aiortc 1.15.0`, `PyAV 17.1.0`, and `numpy 2.5.1` import successfully.
- `pip check` passes and `server.py` compiles with `py_compile`.
- `server.py --help` runs.
- The server was started on `127.0.0.1:8765`; `GET /` returned the expected browser page.

Not yet validated:

- A real phone WebRTC connection over trusted HTTPS.
- A real OBS Media Source receiving the stream.
- Glass-to-glass latency, A/V sync, reconnect behavior, packet loss behavior, and encoder CPU headroom on deployment hardware.
- A local FFmpeg SRT loopback was attempted, but isolated command sessions in this execution environment could not connect their temporary localhost listener and caller. Treat that result as an environment limitation, not a pipeline test.

## Architectural Decisions

| Decision | Reason |
| --- | --- |
| WebRTC terminates at aiortc | Browsers natively capture and transport low-latency camera and microphone media. aiortc exposes decoded frames for FFmpeg. |
| Use SRT, MPEG-TS, and OBS listener mode | SRT gives a small configurable recovery buffer and avoids RTMP latency. OBS as listener lets the server make the outbound connection. |
| No RTMP, nginx-rtmp, or second relay | They add latency and operational complexity without helping the single-LAN-stream Phase 1A scope. |
| Two direct OS pipes into one FFmpeg process | Raw `yuv420p` video and packed `s16le` audio avoid an intermediate media server and Python queue. |
| Non-blocking writes with `asyncio` writer notifications | `write_pipe()` uses `os.write()` and `loop.add_writer()` rather than a writer thread or a producer queue. This bounds Python memory use. |
| Start FFmpeg after first video frame | Raw video needs width and height before FFmpeg starts. The first `VideoFrame` is the source of truth. |
| Fixed first-frame geometry | FFmpeg's raw-video input has one geometry. Later changed-size frames are reformatted to the original geometry. |
| 48 kHz stereo PCM for FFmpeg input | Browser audio is normalized with `AudioResampler` so FFmpeg gets a stable `s16le`, 48 kHz, 2-channel input. |
| `libx264` with `ultrafast` and `zerolatency` | This minimizes encoder work and lookahead on broadly available CPU-only FFmpeg builds. |
| One-second GOP, no B-frames, one reference | Fast OBS acquisition/recovery and no frame reordering delay. |
| No FFmpeg `-re` | aiortc already supplies live pacing. `-re` would add pacing delay. |
| One application session | A new offer closes the old `StreamSession`, including its FFmpeg process. This is explicit single-user behavior. |
| No STUN/TURN | The client has `iceServers: []`; this is a LAN-only implementation. TURN is deferred to Phase 1B. |
| HTTPS signaling server | Mobile browsers require a secure context for camera and microphone access except at localhost. |

## Runtime Contract

### HTTP(S) API

`GET /`

- Returns `index.html`.
- Served by aiohttp from the same origin as signaling.

`POST /offer`

- Request: JSON WebRTC offer, for example `{"type":"offer","sdp":"..."}`.
- Response: JSON WebRTC answer with the same fields.
- The browser waits for ICE gathering to complete before posting. There is no trickle ICE endpoint.
- A malformed body returns HTTP 400. A peer-connection setup failure returns HTTP 500.
- There is no authentication, authorization, CORS configuration, rate limit, or session token.

### Ports and Protocols

| Default | Protocol | Direction | Purpose |
| --- | --- | --- | --- |
| TCP 8443 | HTTPS | Phone -> Python server | Page delivery and WebRTC offer/answer signaling. |
| UDP dynamic | WebRTC ICE / DTLS-SRTP | Phone <-> Python server | Media. Host candidates only in this phase. Firewall policy must allow the negotiated UDP flow. |
| UDP 9000 | SRT | Python server -> OBS | MPEG-TS contribution. OBS listens; FFmpeg calls. |

The default sender SRT URL is:

```text
srt://127.0.0.1:9000?mode=caller&latency=120000&peerlatency=120000&transtype=live
```

Replace `127.0.0.1` with the OBS LAN address when OBS is on another host. FFmpeg SRT latency values are microseconds: `120000` is 120 ms. The OBS Media Source listener URI is normally:

```text
srt://0.0.0.0:9000?mode=listener&latency=120000
```

### Certificates

`--cert-file` and `--key-file` must be supplied together and must be PEM files. The certificate must cover the exact LAN hostname or IP used by the phone. A locally trusted CA such as `mkcert` is appropriate for development; install its CA on the phone. A browser certificate bypass is not a dependable way to enable `getUserMedia`.

WebRTC media itself uses DTLS-SRTP. SRT encryption is not configured by default; add supported SRT passphrase parameters to `--srt-url` only after testing FFmpeg and OBS interoperability.

### Dependencies

- System: Python 3.11+, FFmpeg with `libsrt`, `libx264`, AAC, MPEG-TS, OBS Studio with SRT-capable FFmpeg, and a POSIX-like server OS.
- Python: `aiohttp`, `aiortc`, `numpy`; PyAV is installed transitively by aiortc and is imported directly by the server.
- Local environment created in this session: `.venv`.

## File Guide

| File | Purpose |
| --- | --- |
| `server.py` | Entire backend: CLI, HTTPS aiohttp app, `/offer`, aiortc peer/session ownership, frame conversion, non-blocking pipes, FFmpeg lifecycle, and shutdown. |
| `index.html` | Single-page mobile client: capture constraints, preview, same-origin signaling, and reconnect behavior. No build step. |
| `requirements.txt` | Python dependency constraints. NumPy is required by `VideoFrame.to_ndarray()` and `AudioFrame.to_ndarray()`. |
| `README.md` | Original project overview and user-facing deployment instructions. |
| `QUICKSTART.md` | Short, exact first live-test procedure. |
| `ARCHITECTURE.md` | Detailed component, data-flow, media, networking, and latency documentation. |
| `TODO.md` | Prioritized remaining work. |
| `DEVELOPMENT_LOG.md` | Chronological record of work and validation performed in this session. |
| `HANDOFF.md` | This document; start here when taking ownership. |

## Important Implementation Details

### `server.py`

`FfmpegBridge.start()` creates two `os.pipe()` pairs. The read ends are inherited by FFmpeg through `pass_fds`; the server retains only non-blocking write ends. FFmpeg receives:

```text
pipe:<video-fd> -> rawvideo, yuv420p, first-frame width/height, configured fps
pipe:<audio-fd> -> s16le, 48000 Hz, 2 channels
```

`write_pipe()` retries partial writes and awaits pipe writability. It does not create a producer queue. `write_video()` converts every incoming decoded video frame with `frame.to_ndarray(format="yuv420p")`, then copies bytes into the pipe. `write_audio()` uses a per-track `AudioResampler` and writes packed PCM.

Key FFmpeg choices are `-fflags +nobuffer`, `-flags +low_delay`, one-entry input thread queues, `-preset ultrafast`, `-tune zerolatency`, `-g fps * gop_seconds`, `-bf 0`, `-refs 1`, `-muxdelay 0`, `-muxpreload 0`, `-flush_packets 1`, `-mpegts_flags +resend_headers`, and `-pat_period 0.1`.

`StreamSession` owns an `RTCPeerConnection`, a bridge, and track-consumer tasks. It cancels the other task before shutting down the peer and FFmpeg. `ContributionServer` serializes offer replacement using one `asyncio.Lock`.

### `index.html`

The browser asks for ideal 1280x720, a maximum 30 fps, rear-facing camera preference, and rawish microphone capture with echo cancellation, noise suppression, and automatic gain control disabled. It sends audio and video tracks in one peer connection. It gathers host ICE candidates before `POST /offer`. On `failed`, `disconnected`, or unexpected `closed`, it retries after one second. `stop()` prevents reconnecting and stops the browser media tracks.

## Known Bugs and Limitations

1. **No real end-to-end test yet.** Do not claim the 400-800 ms target is achieved until a phone, server, and OBS glass-to-glass test is measured.
2. **POSIX event-loop dependency.** `write_pipe()` uses `loop.add_writer()`, `os.pipe()`, and `pass_fds`. It is intended for Linux/POSIX selector-style event loops. It is not tested on Windows and may fail under the default Windows Proactor loop.
3. **Backpressure can still become latency.** No Python queue is retained, but if FFmpeg cannot encode fast enough, a write blocks and aiortc may buffer upstream frames. There is no latest-frame drop policy or overload metric yet.
4. **Initial audio waits for video start.** FFmpeg waits for the first video frame to learn geometry. Audio received before that waits in the audio consumer; startup A/V timing has not been measured.
5. **FFmpeg failures are not supervised.** stderr is logged, but there is no child-process watcher, restart policy, or health endpoint. A failed encoder ultimately tears down the session on a subsequent pipe write.
6. **Reconnect causes an SRT interruption.** A new offer closes the old FFmpeg process. There is no make-before-break handoff to keep OBS continuously fed.
7. **Single stream only.** A second browser replaces the first. There is no user identity, token, room, or stream key.
8. **LAN ICE only.** No STUN/TURN means routed networks, NAT boundaries, and mobile WAN use are unsupported.
9. **No SRT encryption or authentication.** The SRT URL is operator-controlled CLI input; it defaults to clear SRT transport.
10. **CPU encoder only.** `libx264` may not sustain high resolutions on small CPUs. Hardware encoding is not implemented or benchmarked.
11. **Fixed resolution after startup.** Mid-stream camera rotation or dimension changes are reformatted, which costs CPU and may distort if aspect ratio changes.
12. **No automated tests.** There are only manual smoke checks. Browser/OBS integration, reconnect, error handling, and A/V synchronization need coverage.
13. **Minor code hygiene.** `server.py` imports `signal` but does not use it. Remove it in a cleanup change.

## Remaining Work

Read `TODO.md` for the prioritized list. Immediate work is a real LAN E2E test and instrumentation, not broad refactoring.

## Exact Commands

### Install host prerequisites (Ubuntu/Debian)

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3-venv
ffmpeg -protocols | grep srt
ffmpeg -encoders | grep -E 'libx264|aac'
```

### Create/update Python environment

```bash
cd /home/salem/webrtc-srt-obs
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip check
```

### Create a LAN certificate with mkcert

```bash
hostname -I
mkcert -install
mkcert -cert-file cert.pem -key-file key.pem 192.168.1.20 localhost
```

Replace `192.168.1.20` with the server LAN IP. Install the mkcert root CA on the phone.

### Configure OBS

1. Add **Media Source**.
2. Clear **Local File**.
3. Input: `srt://0.0.0.0:9000?mode=listener&latency=120000`.
4. Start with network buffering at 100 ms or the lowest stable value available.
5. Leave the source active.

### Run the contribution server

```bash
cd /home/salem/webrtc-srt-obs
.venv/bin/python server.py \
  --host 0.0.0.0 \
  --port 8443 \
  --cert-file cert.pem \
  --key-file key.pem \
  --srt-url 'srt://127.0.0.1:9000?mode=caller&latency=120000&peerlatency=120000&transtype=live' \
  --log-level INFO
```

For OBS on another machine, replace the SRT host with that machine's LAN IP. On the phone, open `https://SERVER_LAN_IP:8443/` and select **Start stream**.

### Debug

```bash
# More server and FFmpeg stderr detail
.venv/bin/python server.py --host 0.0.0.0 --port 8443 --cert-file cert.pem --key-file key.pem --log-level DEBUG

# Syntax and dependency checks
.venv/bin/python -m py_compile server.py
.venv/bin/python -c 'import aiohttp, aiortc, av, numpy; print("runtime imports: OK")'
.venv/bin/pip check

# HTTP-only local page smoke test; it intentionally cannot use phone media
.venv/bin/python server.py --host 127.0.0.1 --port 8765
curl --fail http://127.0.0.1:8765/

# Inspect incoming SRT without OBS, on a real host/network
ffprobe -loglevel info -show_streams 'srt://0.0.0.0:9000?mode=listener&latency=120000'
```

## Assumptions Future Contributors Must Preserve or Revisit Deliberately

- Phase 1A is not a multi-user service. Do not add scaling infrastructure before completing measured E2E validation.
- OBS is the SRT listener and FFmpeg is the caller unless the operational deployment requires the reverse. Change both URI configurations together.
- Camera/microphone use requires trusted HTTPS on a phone. HTTP is only useful for localhost page smoke checks.
- The browser, server, and OBS should be on a reliable low-loss LAN. There is no TURN fallback.
- Keep capture FPS, FFmpeg `--fps`, GOP calculation, and performance budget aligned. The default is 30 fps and a 30-frame GOP.
- SRT latency units in FFmpeg query parameters are microseconds. Do not accidentally set `120` when the intent is 120 ms.
- Add benchmark evidence before replacing `libx264` with a hardware encoder; some hardware paths introduce more buffering than this CPU baseline.
- Do not introduce a Python `asyncio.Queue` between tracks and FFmpeg without a bounded latest-frame/drop policy and measured latency impact.

## Session History

The detailed chronological record is in `DEVELOPMENT_LOG.md`.
