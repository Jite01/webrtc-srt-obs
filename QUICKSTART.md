# Quick Start

This procedure produces the first real phone-to-OBS test. It assumes phone, Python server, and OBS are on the same trusted LAN.

## 1. Install prerequisites

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg python3-venv
ffmpeg -protocols | grep srt
ffmpeg -encoders | grep -E 'libx264|aac'
```

The final two commands must show SRT, `libx264`, and AAC.

## 2. Install Python dependencies

```bash
cd /home/salem/webrtc-srt-obs
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip check
```

## 3. Create a trusted LAN certificate

Phone media capture requires HTTPS. Determine the server LAN address:

```bash
hostname -I
```

With mkcert installed, create a certificate for that address:

```bash
mkcert -install
mkcert -cert-file cert.pem -key-file key.pem 192.168.1.20 localhost
```

Replace `192.168.1.20` with the address returned by `hostname -I`. Install/trust the mkcert root CA on the phone. Do not use an HTTP URL for the phone test.

## 4. Configure OBS first

1. In OBS, add **Media Source**.
2. Clear **Local File**.
3. Enter this input URL:

   ```text
   srt://0.0.0.0:9000?mode=listener&latency=120000
   ```

4. Set OBS network buffering to about 100 ms initially, or the lowest stable value supported by the installed OBS version.
5. Keep the source active. OBS is now waiting for the server.

## 5. Start the server

When OBS is on the same machine:

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

When OBS is on another machine, use its LAN IP in `--srt-url`, for example:

```bash
--srt-url 'srt://192.168.1.30:9000?mode=caller&latency=120000&peerlatency=120000&transtype=live'
```

## 6. Start the phone contribution

1. On the phone, open `https://SERVER_LAN_IP:8443/`.
2. Accept the trusted certificate and grant camera/microphone permission.
3. Select **Start stream**.
4. The page displays local preview and connection state.
5. Verify the OBS Media Source receives video and audio.

## 7. First tuning pass

Leave all defaults in place for the first measurement: 720p30 preference, 3.5 Mb/s video, 96 kb/s audio, 1-second GOP, and 120 ms SRT latency. Measure glass-to-glass delay with a clock in the phone camera view.

Only change one parameter at a time:

- Clean LAN, stable output: try SRT latency `80000` and reduce OBS buffering cautiously.
- Wi-Fi artifacts or dropouts: return to `120000` or increase it.
- CPU overload: reduce `--video-bitrate`; if needed, constrain camera resolution in `index.html` after measurement.

## Troubleshooting

| Symptom | First checks |
| --- | --- |
| Phone cannot grant camera/mic | Confirm HTTPS, certificate trust, and exact certificate hostname/IP match. |
| Browser says reconnect pending | Confirm phone/server same LAN and firewall allows direct WebRTC UDP. This phase has no TURN. |
| Server logs no `track_received` | Inspect browser connection state and `/offer` response in mobile browser remote debugging. |
| FFmpeg fails at start | Run `ffmpeg -protocols | grep srt` and `ffmpeg -encoders | grep libx264`; inspect server `--log-level DEBUG` output. |
| OBS receives nothing | Start OBS listener first; verify SRT host/UDP port; ensure server sends to the OBS LAN address. |
| Latency too high | Measure all buffers, then inspect OBS network buffer, SRT latency, Wi-Fi quality, and CPU load before changing encoder flags. |
| A/V drifts or stutters | Record server logs and exact versions; this has not yet been tuned on real equipment. |

## Useful Checks

```bash
cd /home/salem/webrtc-srt-obs
.venv/bin/python -m py_compile server.py
.venv/bin/python -c 'import aiohttp, aiortc, av, numpy; print("runtime imports: OK")'
.venv/bin/pip check
```

For a page-only local smoke test, which deliberately has no phone-media capability without HTTPS:

```bash
.venv/bin/python server.py --host 127.0.0.1 --port 8765
curl --fail http://127.0.0.1:8765/
```
