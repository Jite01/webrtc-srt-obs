# Remaining Work

Priority reflects risk to achieving the Phase 1A latency objective, not implementation convenience.

## P0: Validate the existing path on real equipment

- [ ] Run phone -> server -> OBS on the target LAN using a trusted certificate and the documented SRT listener configuration.
- [ ] Measure glass-to-glass latency with a visible millisecond clock or flash test. Record phone model, browser/version, server CPU, Wi-Fi topology, OBS version, resolution, FPS, bitrate, SRT latency, OBS network buffer, and measured result.
- [ ] Verify video and audio are present, lip sync is acceptable, and OBS sees H.264/AAC MPEG-TS reliably.
- [ ] Exercise phone lock/unlock, browser background/foreground, Wi-Fi reconnect, browser reload, and server restart. Record expected OBS behavior and recovery time.
- [ ] Confirm actual firewall rules permit HTTPS TCP 8443, direct WebRTC UDP, and SRT UDP 9000 for the chosen topology.

## P1: Make the current pipeline observable and resilient

- [ ] Add explicit FFmpeg process-exit monitoring, reason logging, and a clear browser/session failure response. Do not silently rely on a later pipe write to discover child exit.
- [ ] Add `/healthz` and a read-only status endpoint with peer state, FFmpeg PID/return code, negotiated video geometry, and frame/error counters. Keep it unauthenticated only while LAN-only.
- [ ] Add structured counters and periodic metrics for received frames, converted frames, pipe-write blocking time, dropped frames, FFmpeg restarts, and connection state changes.
- [ ] Measure encode and pipe latency under load. Design a bounded latest-frame drop policy only if actual backpressure makes latency grow; do not add an unbounded queue.
- [ ] Add automated unit tests for CLI validation, offer errors, session replacement, command construction, pipe closure, and client signaling behavior.
- [ ] Add an integration test using a real OS network namespace or container setup that proves FFmpeg SRT caller/listener interoperability. The current execution sandbox cannot provide this.
- [ ] Remove the unused `signal` import in `server.py` as part of a small lint/cleanup pass.

## P1: Improve operational deployment

- [ ] Add a documented `systemd` service with least-privilege user, restart policy, working directory, certificate paths, and logging guidance.
- [ ] Define a certificate renewal and phone trust process. Decide whether a private LAN CA or a publicly trusted DNS name is operationally appropriate.
- [ ] Add a launch-time FFmpeg capability check with a clear failure message for absent SRT, `libx264`, AAC, or unsupported FFmpeg build options.
- [ ] Add an operator configuration file or environment-variable layer only after the actual deployed settings are known. Preserve simple CLI overrides.

## P2: Phase 1B network and security scope

- [ ] Add authenticated signaling and short-lived stream credentials.
- [ ] Add TURN and STUN configuration for non-LAN / NAT traversal, with diagnostics surfaced in the browser and server logs.
- [ ] Add an SRT encryption/passphrase policy and confirm OBS/FFmpeg compatibility.
- [ ] Restrict signaling sources and add rate limiting before exposing outside a trusted LAN.
- [ ] Define session ownership and explicit replacement behavior before allowing multiple users.

## P2: Media quality and latency tuning

- [ ] Benchmark `libx264` at target resolutions and bitrates. Verify CPU headroom under real Wi-Fi load.
- [ ] Evaluate hardware encoders only with measured encoder, mux, decoder, and OBS buffering latency. Do not assume hardware encoding is lower latency.
- [ ] Add bitrate/resolution/FPS profiles grounded in measurements, not device guesses.
- [ ] Add timestamp-aware A/V synchronization checks and a repeatable test signal.
- [ ] Decide whether a shorter GOP or lower SRT latency materially improves production results without destabilizing video.
- [ ] Consider a continuous output/reconnect handoff only after a measured requirement exists; it adds meaningful complexity.

## Non-goals Until Requirements Change

- Multi-stream routing or horizontal scaling.
- RTMP or nginx-rtmp integration.
- A generic front-end build system or UI framework.
- Recording, replay, transcoding ladders, or CDN distribution.
