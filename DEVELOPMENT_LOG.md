# Development Log

## 2026-07-15: Phase 1A implementation

1. Reviewed the requested architecture and rejected RTMP/nginx-rtmp for this phase because they would dominate the latency budget.
2. Selected the final path: phone browser WebRTC -> aiortc decoded frames -> one FFmpeg child -> MPEG-TS/SRT -> OBS Media Source.
3. Chose HTTPS aiohttp signaling rather than a separate signaling service. Mobile camera/microphone access requires a secure origin.
4. Implemented `server.py` with:
   - aiohttp `GET /` and `POST /offer` routes;
   - single-session replacement semantics;
   - aiortc audio/video track consumers;
   - non-blocking direct OS-pipe writes using `asyncio` descriptor readiness;
   - first-video-frame FFmpeg startup to determine raw geometry;
   - FFmpeg low-latency `libx264`, AAC, MPEG-TS, and SRT settings;
   - process stderr logging and bounded shutdown.
5. Implemented `index.html` with local preview, mobile capture constraints, complete ICE gathering before offer submission, status display, stop control, and reconnect handling.
6. Added `requirements.txt` and `README.md` with installation, certificate, OBS, SRT, and latency guidance.
7. Corrected a reconnect race in the browser: stale peer callbacks can no longer affect a newer peer connection.
8. Added one-entry FFmpeg input thread queues to make the low-buffering configuration explicit.
9. Added an HTTPS-disabled warning in `server.py` to make HTTP-only launches visibly unsuitable for phone capture.

## 2026-07-15: Host setup and validation

1. Initial FFmpeg capability check found no `ffmpeg` executable.
2. The host operator installed FFmpeg normally with the OS package manager. Verified installed FFmpeg version: `8.0.1-3ubuntu2`.
3. Verified FFmpeg reports SRT protocol plus `libx264` and AAC encoders.
4. Initial `.venv` creation failed because the OS `venv` package was absent.
5. The host operator installed the Python venv package. Created project-local `.venv` with Python `3.14.4`.
6. Installed `aiohttp`, `aiortc`, and transitive PyAV dependencies.
7. Discovered that PyAV frame `.to_ndarray()` needs NumPy but NumPy is not installed transitively in this environment. Added `numpy>=1.26,<3` to `requirements.txt` and installed it.
8. Verified:
   - `aiohttp 3.14.1`, `aiortc 1.15.0`, `av 17.1.0`, and `numpy 2.5.1` import;
   - PyAV creates `yuv420p` arrays of expected shape and packed stereo PCM arrays;
   - `python -m py_compile server.py` passes;
   - `pip check` reports no broken requirements;
   - `server.py --help` runs.
9. Started the application on `127.0.0.1:8765` without TLS for a page-only smoke test. `curl` confirmed the expected title, preview video element, and start button. This does not validate browser media because phone capture requires HTTPS.
10. Attempted a local SRT loopback with a temporary FFmpeg listener and a temporary Python bridge harness. The execution platform isolates command sessions such that the local listener/caller did not establish a usable connection. The temporary harness was deleted. No conclusion about the application's SRT path should be drawn from that failed environment-only loopback.

## Documentation Handoff

On 2026-07-15, added `HANDOFF.md`, `ARCHITECTURE.md`, `DEVELOPMENT_LOG.md`, `TODO.md`, and `QUICKSTART.md` so future contributors can continue without access to this conversation.
