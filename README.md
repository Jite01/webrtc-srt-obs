# WebRTC to SRT OBS Contribution (Phase 1A)

This is a single-phone, single-OBS contribution pipeline designed for low latency on a reliable LAN:

```text
Phone browser -- WebRTC (audio + video) --> aiortc server
                                             | decoded frames
                                             v
                                      FFmpeg / libx264 + AAC
                                             | MPEG-TS over SRT
                                             v
                                      OBS Media Source (listener)
```

RTMP and nginx are deliberately not part of this project. The server starts one FFmpeg process when it sees the first camera frame, passes raw `yuv420p` video and 48 kHz stereo PCM through separate non-blocking OS pipes, and applies backpressure instead of accumulating Python queues. FFmpeg encodes H.264 with `ultrafast` and `zerolatency`, no B-frames, a one-second GOP, small rate-control buffer, immediate MPEG-TS flushing, and SRT live transport.

## Requirements

- Python 3.11 or newer
- FFmpeg built with `libx264`, `aac`, `mpegts`, and `libsrt` support
- OBS Studio with FFmpeg/SRT support
- Phone and server on the same LAN for this phase
- A trusted HTTPS certificate for the server hostname or IP address

Verify SRT support before testing:

```bash
ffmpeg -protocols | grep srt
ffmpeg -encoders | grep libx264
```

## Installation

```bash
cd /home/salem/webrtc-srt-obs
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Camera and microphone access on a phone requires HTTPS. For LAN development, create a locally trusted certificate with [mkcert](https://github.com/FiloSottile/mkcert), including the server LAN IP address:

```bash
mkcert -install
mkcert 192.168.1.20 localhost
```

Install the generated CA on the phone as a trusted certificate authority. A browser warning accepted for a self-signed certificate is not sufficient for reliable `getUserMedia` access.

## OBS configuration

1. Add a **Media Source** in OBS.
2. Uncheck **Local File**.
3. Set **Input** to `srt://0.0.0.0:9000?mode=listener&latency=120000`.
4. Set **Network Buffering** to the lowest stable value available in the OBS version, starting at `100 ms`.
5. Keep the source active and start the server afterwards.

The server is an SRT caller and OBS is the listener, which is the simplest topology when OBS receives the contribution. `latency` is in microseconds in FFmpeg's SRT URL, so `120000` is 120 ms. A clean LAN can often use `80000`; increase it if packet loss causes artifacts.

## Run

Replace the certificate paths, server LAN address, and OBS host if OBS runs on another machine:

```bash
.venv/bin/python server.py \
  --host 0.0.0.0 \
  --port 8443 \
  --cert-file 192.168.1.20+1.pem \
  --key-file 192.168.1.20+1-key.pem \
  --srt-url 'srt://127.0.0.1:9000?mode=caller&latency=120000&peerlatency=120000&transtype=live'
```

Open `https://192.168.1.20:8443/` on the phone, accept camera and microphone access, then select **Start stream**. The page keeps local media tracks alive and automatically sends a new offer after a transient WebRTC disconnect. A new offer replaces the existing stream; that is intentional for the one-stream scope.

The default 3.5 Mb/s video rate targets 720p30. For constrained Wi-Fi, start with `--video-bitrate 2000k`; for a clean network and higher-quality 720p, use `5000k`. Keep `--fps 30` aligned with the browser capture constraint.

## Latency and data-flow decisions

- WebRTC is terminated at aiortc, so media is decoded once before the required FFmpeg encode.
- There are no application queues. Each track writes directly to FFmpeg's pipe; operating-system pipe backpressure prevents unbounded memory growth.
- The browser requests 720p30 and does not apply audio-processing effects, avoiding avoidable phone-side delay.
- FFmpeg is not given `-re`: aiortc already provides live pacing, and `-re` would add delay.
- The transport is MPEG-TS/SRT, not RTMP. It provides packet recovery and a small, configurable receiver buffer.
- A short GOP gives OBS rapid acquisition and recovery. Disabling B-frames and encoder lookahead avoids reorder delay.

The expected 400-800 ms end-to-end range assumes a stable LAN, a reasonably fast server, hardware-independent `libx264` encoding at the chosen resolution, and OBS buffering tuned near 100 ms. Measure glass-to-glass latency with a visible clock before changing multiple parameters at once.

## Known limitations

- LAN only: there is no TURN server, NAT traversal, authentication, or encryption policy beyond browser HTTPS.
- Exactly one browser connection is allowed. A new connection stops the current FFmpeg process and replaces it.
- Video resolution is fixed by the first frame. Camera rotation or mid-stream resolution changes are scaled to that geometry.
- This phase uses CPU `libx264`; use a hardware encoder only after measuring its encoder and decoder buffering behavior on the target hardware.
- Raw-frame transfer from aiortc to FFmpeg necessarily includes pixel-format conversion and a memory copy. It is the required boundary between decoded WebRTC frames and FFmpeg in this architecture.
- OBS SRT/Media Source behavior varies slightly by OBS and bundled FFmpeg version.

