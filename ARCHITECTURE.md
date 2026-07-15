# Architecture

## Scope

This is Phase 1A: one phone browser, one Python process, one FFmpeg child process, and one OBS instance. The system prioritizes a simple, inspectable low-latency path over scale, authentication, or internet traversal.

## Component Diagram

```text
                         TCP 8443, HTTPS
   +---------------+  GET / and POST /offer   +-------------------+
   | Phone browser | ------------------------> | aiohttp in        |
   | index.html    | <------------------------ | server.py         |
   +-------+-------+      answer JSON          +---------+---------+
           |                                             |
           | UDP, ICE + DTLS-SRTP                         | aiortc decodes
           +--------------------------------------------->|
                                                         v
                                              +-------------------+
                                              | StreamSession     |
                                              | video/audio tasks |
                                              +---------+---------+
                                                        |
                              raw yuv420p / s16le through OS pipes
                                                        v
                                              +-------------------+
                                              | FFmpeg            |
                                              | libx264 + AAC     |
                                              +---------+---------+
                                                        |
                                           UDP SRT / MPEG-TS, port 9000
                                                        v
                                              +-------------------+
                                              | OBS Media Source  |
                                              | SRT listener      |
                                              +-------------------+
```

## Control Plane

The browser and server use HTTP(S) only for SDP exchange. `index.html` creates an `RTCPeerConnection` with no configured ICE servers, adds its camera and microphone tracks, creates an offer, waits until ICE gathering is complete, and posts the complete SDP to `/offer`.

The server's `ContributionServer.offer()` accepts one offer at a time under an `asyncio.Lock`. It closes an existing session, creates a new `StreamSession`, calls `setRemoteDescription()`, creates and sets a local answer, and returns the answer JSON. There is no trickle ICE, websocket, renegotiation endpoint, or data channel.

This is deliberately same-origin signaling. The page is served from `/`, so `fetch('/offer')` does not need CORS configuration.

## Media Plane

### Browser capture

The browser requests:

- Video: rear camera preferred, ideal 1280x720, maximum 30 fps.
- Audio: microphone with echo cancellation, noise suppression, and auto gain disabled.

These are constraints/preferences, not guarantees; the browser or phone can select a different compatible format. Browser WebRTC encodes and sends the tracks using its negotiated codecs. aiortc decodes received media into PyAV frames.

### Server receive and conversion

The `track` event schedules one task per media type:

- `_consume_video()` receives `VideoFrame` objects. The first video frame starts FFmpeg and fixes raw input width and height. Each frame is converted to contiguous `yuv420p` data via `to_ndarray()`.
- `_consume_audio()` receives `AudioFrame` objects. It uses a dedicated `AudioResampler(format="s16", layout="stereo", rate=48000)` and writes each converted output frame.

There is an unavoidable copy at the decoded-frame-to-raw-pipe boundary. It is a consequence of passing decoded aiortc/PyAV frames to a separate FFmpeg process. Avoiding it would require a different architecture, such as codec packet forwarding, which is not appropriate here because codec compatibility and audio/video handling would be more complex.

### Pipe and process model

`FfmpegBridge` creates independent video and audio pipes. Read descriptors are inherited by the FFmpeg child with `pass_fds`; write descriptors remain non-blocking in the event-loop process. There are no named FIFOs or TCP loopbacks.

`write_pipe()` writes all bytes, handling partial writes. On `EAGAIN` it awaits the file descriptor's writable event rather than allocating an unbounded queue or invoking a worker thread. This is low-overhead on a POSIX selector event loop.

The consequence is intentional backpressure. Healthy FFmpeg consumes faster than media arrives. Under encoder overload, track consumption stalls rather than memory growing without bound. This preserves process health but does not alone guarantee minimum latency under overload; later work should add measured overload handling and controlled frame dropping.

### FFmpeg graph

The command is created dynamically because raw video geometry is unknown until first frame:

```text
raw yuv420p pipe, WIDTHxHEIGHT, FPS ----\
                                           > map video + audio -> H.264/AAC -> MPEG-TS -> SRT caller
s16le 48000 Hz stereo pipe -------------/
```

Encoding settings:

| Area | Settings | Purpose |
| --- | --- | --- |
| Input buffering | `-fflags +nobuffer`, `-flags +low_delay`, `-thread_queue_size 1` | Avoid deliberate demux/input queues. |
| Video | `libx264`, `ultrafast`, `zerolatency`, `baseline`, `yuv420p` | Fast broadly compatible H.264. |
| Rate control | `-b:v` and `-maxrate` from CLI, `-bufsize 350k` | Small encoder rate-control buffer. |
| GOP | `-g FPS*seconds`, `-keyint_min` same, `-sc_threshold 0` | Predictable periodic keyframes. |
| Reordering | `-bf 0`, `-refs 1` | No B-frame reorder delay. |
| Audio | AAC, 48 kHz stereo | OBS-compatible audio alongside H.264. |
| Mux | MPEG-TS, `-muxdelay 0`, `-muxpreload 0`, `-flush_packets 1`, repeated headers | Fast startup and recovery. |
| Transport | SRT `mode=caller`, `transtype=live` | FFmpeg actively connects to OBS listener. |

The code does not set `-re`. The input arrives in real time already; `-re` would slow FFmpeg reads and add latency.

## Lifecycle and Failure Handling

1. Browser starts capture and posts offer.
2. Server installs remote SDP and returns answer.
3. aiortc emits media track events.
4. First video frame starts FFmpeg. Audio waits for bridge readiness.
5. Frame tasks write directly to the pipe descriptors.
6. On track error, bridge pipe closure, or peer failure/closure, `StreamSession.close()` closes peer connection, cancels companion task, closes pipe descriptors, terminates FFmpeg, and drains its stderr task.
7. A new offer replaces the prior session under the server lock.
8. Browser retries one second after a non-intentional `failed`, `disconnected`, or `closed` connection state.

The lifecycle is intentionally simple but not seamless: a reconnect creates a new FFmpeg output and therefore can interrupt OBS briefly.

## Network Model

### HTTPS

The server defaults to `0.0.0.0:8443`. Provide `--cert-file` and `--key-file` for phone use. The backend warns if launched without them. The certificate must be trusted by the phone and match the address opened in the phone browser.

### WebRTC

No STUN or TURN server is used. `iceServers: []` results in host candidates only. Use the same LAN and confirm firewalls allow direct UDP connectivity. This is a non-negotiable Phase 1A boundary.

### SRT

OBS listens at a known UDP port; default documentation uses 9000. FFmpeg's caller URL points at that address. SRT's `latency` and `peerlatency` URL query values are microseconds. Start at 120000 on a clean LAN and tune only after measurement. OBS network buffering adds independently to the total path delay.

## Latency Budget

The target budget is approximate and only valid after measurement:

| Stage | Expected range |
| --- | --- |
| Phone capture/browser WebRTC | 100-200 ms |
| aiortc decoding and conversion | 50-100 ms |
| FFmpeg encode/mux | 100-200 ms |
| SRT target latency and OBS buffering | 150-300 ms |
| Total | about 400-800 ms |

Important sources of extra latency are Wi-Fi retries, browser capture processing, aiortc buffering after pipe backpressure, CPU saturation, rate-control buffering, SRT latency, OBS network buffering, OBS scene/render delay, and display latency. Use a visible clock or flashed frame test for glass-to-glass measurement.

## Security Boundaries

- HTTPS protects page delivery and signaling when a valid certificate is used.
- WebRTC media uses DTLS-SRTP.
- SRT has no configured passphrase/encryption in the default URL.
- There is no application authentication or authorization.
- The server accepts a single peer but does not restrict source IPs.

Do not expose this directly to an untrusted network. Add authentication, authorization, SRT passphrase policy, and TURN before any non-LAN deployment.

## Portability

The bridge is designed for Linux/POSIX. It relies on `os.pipe`, descriptor inheritance, and `asyncio` selector `add_writer`. Validate or refactor the transport bridge before supporting Windows or a non-selector event loop.
