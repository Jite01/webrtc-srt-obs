#!/usr/bin/env python3
"""Single-phone WebRTC to low-latency SRT contribution server."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
import errno
import logging
import os
import signal
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web
from aiortc import AudioStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import AudioFrame, AudioResampler, VideoFrame

LOG = logging.getLogger("webrtc_srt")
ROOT = Path(__file__).resolve().parent

# Repository lookup order:
#   1. DEEP_LIVE_CAM_ROOT environment variable
#   2. Local fork (preferred)
#   3. Upstream clone (fallback)
_env_root = os.environ.get("DEEP_LIVE_CAM_ROOT")

if _env_root:
    DLC_ROOT = Path(_env_root).expanduser().resolve()
else:
    candidates = (
        ROOT.parent / "hacksider_Deep-Live-Cam",
        ROOT.parent / "Deep-Live-Cam-upstream",
    )

    DLC_ROOT = next((p.resolve() for p in candidates if p.exists()), None)

    if DLC_ROOT is None:
        raise RuntimeError(
            "Deep-Live-Cam repository not found.\n"
            "Either set DEEP_LIVE_CAM_ROOT or clone "
            "'hacksider_Deep-Live-Cam' beside this project."
        )

inference_file = DLC_ROOT / "inference.py"
if not inference_file.exists():
    raise FileNotFoundError(
        f"Expected inference.py in {DLC_ROOT}"
    )

if str(DLC_ROOT) not in sys.path:
    sys.path.insert(0, str(DLC_ROOT))

LOG.info("Using Deep-Live-Cam repository: %s", DLC_ROOT)

from inference import InferenceEngine


class BridgeClosedError(RuntimeError):
    """The media bridge stopped while a track was being consumed."""


async def write_pipe(fd: int, payload: bytes | memoryview) -> None:
    """Write a complete payload without a worker thread or an application queue."""
    view = memoryview(payload)
    loop = asyncio.get_running_loop()

    while view:
        try:
            written = os.write(fd, view)
            view = view[written:]
        except BlockingIOError:
            writable = loop.create_future()

            def on_writable() -> None:
                loop.remove_writer(fd)
                if not writable.done():
                    writable.set_result(None)

            loop.add_writer(fd, on_writable)
            try:
                await writable
            finally:
                loop.remove_writer(fd)
        except OSError as exc:
            if exc.errno in (errno.EPIPE, errno.EBADF):
                raise BridgeClosedError("FFmpeg pipe is closed") from exc
            raise


@dataclass(frozen=True)
class StreamConfig:
    ffmpeg: str
    srt_url: str
    fps: int
    video_bitrate: str
    audio_bitrate: str
    gop_seconds: int


class FfmpegBridge:
    """Starts FFmpeg after the first video frame establishes the input geometry."""

    def __init__(self, config: StreamConfig) -> None:
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.video_fd: int | None = None
        self.audio_fd: int | None = None
        self.width: int | None = None
        self.height: int | None = None
        self._start_lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closed = False
        self._log_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self, width: int, height: int) -> None:
        if width % 2 or height % 2:
            raise ValueError(f"camera resolution must be even for yuv420p, got {width}x{height}")

        async with self._start_lock:
            if self._closed:
                raise BridgeClosedError("bridge was already closed")
            if self.process is not None:
                return

            video_read, video_write = os.pipe()
            audio_read, audio_write = os.pipe()
            os.set_blocking(video_write, False)
            os.set_blocking(audio_write, False)
            gop = self.config.fps * self.config.gop_seconds

            command = [
                self.config.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-nostdin",
                "-fflags",
                "+nobuffer",
                "-flags",
                "+low_delay",
                "-f",
                "rawvideo",
                "-pixel_format",
                "yuv420p",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                str(self.config.fps),
                "-thread_queue_size",
                "1",
                "-i",
                f"pipe:{video_read}",
                "-f",
                "s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-thread_queue_size",
                "1",
                "-i",
                f"pipe:{audio_read}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "baseline",
                "-pix_fmt",
                "yuv420p",
                "-b:v",
                self.config.video_bitrate,
                "-maxrate",
                self.config.video_bitrate,
                "-bufsize",
                "350k",
                "-g",
                str(gop),
                "-keyint_min",
                str(gop),
                "-sc_threshold",
                "0",
                "-bf",
                "0",
                "-refs",
                "1",
                "-c:a",
                "aac",
                "-b:a",
                self.config.audio_bitrate,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-max_muxing_queue_size",
                "4",
                "-muxdelay",
                "0",
                "-muxpreload",
                "0",
                "-flush_packets",
                "1",
                "-mpegts_flags",
                "+resend_headers",
                "-pat_period",
                "0.1",
                "-f",
                "mpegts",
                self.config.srt_url,
            ]

            try:
                self.process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                    pass_fds=(video_read, audio_read),
                )
            except Exception:
                os.close(video_read)
                os.close(video_write)
                os.close(audio_read)
                os.close(audio_write)
                raise

            os.close(video_read)
            os.close(audio_read)
            self.video_fd = video_write
            self.audio_fd = audio_write
            self.width = width
            self.height = height
            self._log_task = asyncio.create_task(self._drain_ffmpeg_logs(), name="ffmpeg-logs")
            self._ready.set()
            LOG.info("ffmpeg_started resolution=%dx%d pid=%s", width, height, self.process.pid)

    async def wait_until_ready(self) -> None:
        await self._ready.wait()
        if not self.running:
            raise BridgeClosedError("FFmpeg did not start")

    async def write_video(self, frame: VideoFrame) -> None:
        await self.wait_until_ready()
        if frame.width != self.width or frame.height != self.height:
            frame = frame.reformat(width=self.width, height=self.height, format="yuv420p")
        pixels = frame.to_ndarray(format="yuv420p")
        await write_pipe(self.video_fd, pixels.tobytes())  # type: ignore[arg-type]

    async def write_audio(self, frame: AudioFrame, resampler: AudioResampler) -> None:
        await self.wait_until_ready()
        for converted in resampler.resample(frame):
            samples = converted.to_ndarray()
            await write_pipe(self.audio_fd, samples.tobytes())  # type: ignore[arg-type]

    async def _drain_ffmpeg_logs(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            LOG.warning("ffmpeg message=%s", line.decode(errors="replace").rstrip())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready.set()

        for fd_name in ("video_fd", "audio_fd"):
            fd = getattr(self, fd_name)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, fd_name, None)

        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

        if self._log_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._log_task
        LOG.info("ffmpeg_stopped")


class StreamSession:
    """Owns the one allowed peer connection and its FFmpeg bridge."""

    def __init__(self, config: StreamConfig, engine: InferenceEngine) -> None:
        self.pc = RTCPeerConnection()
        self.bridge = FfmpegBridge(config)
        self.engine = engine
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlc-inference")
        self.latest_frame: Any | None = None
        self._video_worker: asyncio.Task[None] | None = None
        self.tasks: set[asyncio.Task[None]] = set()
        self.closed = False

        @self.pc.on("connectionstatechange")
        def on_connection_state_change() -> None:
            state = self.pc.connectionState
            LOG.info("peer_connection_state state=%s", state)
            if state in {"failed", "closed"}:
                self._spawn(self.close())

        @self.pc.on("track")
        def on_track(track: VideoStreamTrack | AudioStreamTrack) -> None:
            LOG.info("track_received kind=%s id=%s", track.kind, track.id)
            if track.kind == "video":
                self._spawn(self._consume_video(track))
            elif track.kind == "audio":
                self._spawn(self._consume_audio(track))

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _consume_video(self, track: VideoStreamTrack) -> None:
        try:
            while True:
                frame = await track.recv()
                if not self.bridge.running:
                    await self.bridge.start(frame.width, frame.height)
                # One overwrite slot keeps latency bounded while inference is busy.
                self.latest_frame = frame.to_ndarray(format="bgr24")
                if self._video_worker is None or self._video_worker.done():
                    self._video_worker = asyncio.create_task(self._drain_latest_video(), name="dlc-inference")
                    self.tasks.add(self._video_worker)
                    self._video_worker.add_done_callback(self.tasks.discard)
        except (BridgeClosedError, asyncio.CancelledError):
            raise
        except Exception:
            LOG.exception("video_track_failed")
            await self.close()

    async def _drain_latest_video(self) -> None:
        loop = asyncio.get_running_loop()
        LOG.info("inference_worker_started")
        try:
            while self.latest_frame is not None and not self.closed:
                bgr = self.latest_frame
                self.latest_frame = None
                result = await loop.run_in_executor(self.executor, self.engine.process_frame, bgr)
                output = VideoFrame.from_ndarray(result, format="bgr24").reformat(format="yuv420p")
                await self.bridge.write_video(output)
        except (BridgeClosedError, asyncio.CancelledError):
            raise
        except Exception:
            LOG.exception("inference_worker_failed")
            await self.close()
        finally:
            LOG.info("inference_worker_stopped")

    async def _consume_audio(self, track: AudioStreamTrack) -> None:
        resampler = AudioResampler(format="s16", layout="stereo", rate=48000)
        try:
            while True:
                frame = await track.recv()
                await self.bridge.write_audio(frame, resampler)
        except (BridgeClosedError, asyncio.CancelledError):
            raise
        except Exception:
            LOG.exception("audio_track_failed")
            await self.close()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        for task in tuple(self.tasks):
            if task is not current:
                task.cancel()
        await self.pc.close()
        await self.bridge.close()
        await asyncio.to_thread(self.executor.shutdown, True, cancel_futures=True)
        LOG.info("stream_session_closed")


class ContributionServer:
    def __init__(self, config: StreamConfig, engine: InferenceEngine) -> None:
        self.config = config
        self.engine = engine
        self.session: StreamSession | None = None
        self.lock = asyncio.Lock()

    async def offer(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
            offer = RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
        except (KeyError, TypeError, ValueError) as exc:
            raise web.HTTPBadRequest(text="expected JSON WebRTC offer with sdp and type") from exc

        if offer.type != "offer":
            raise web.HTTPBadRequest(text="only WebRTC offers are accepted")

        async with self.lock:
            if self.session is not None:
                await self.session.close()
            session = StreamSession(self.config, self.engine)
            self.session = session

            try:
                await session.pc.setRemoteDescription(offer)
                answer = await session.pc.createAnswer()
                await session.pc.setLocalDescription(answer)
            except Exception:
                await session.close()
                if self.session is session:
                    self.session = None
                LOG.exception("offer_failed")
                raise web.HTTPInternalServerError(text="could not establish WebRTC connection")

        local = session.pc.localDescription
        assert local is not None
        return web.json_response({"sdp": local.sdp, "type": local.type})

    async def shutdown(self, app: web.Application) -> None:
        async with self.lock:
            if self.session is not None:
                await self.session.close()
                self.session = None
        self.engine.teardown()


def create_app(config: StreamConfig, engine: InferenceEngine) -> web.Application:
    server = ContributionServer(config, engine)
    app = web.Application(client_max_size=1024 * 1024)
    app["contribution_server"] = server
    app.router.add_get("/", lambda request: web.FileResponse(ROOT / "index.html"))
    app.router.add_post("/offer", server.offer)
    app.on_shutdown.append(server.shutdown)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--cert-file", type=Path, help="PEM certificate required for phone camera access")
    parser.add_argument("--key-file", type=Path, help="PEM private key required with --cert-file")
    parser.add_argument("--source", required=True, help="character source image path")
    parser.add_argument("--execution-provider", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument(
        "--srt-url",
        default="srt://127.0.0.1:9000?mode=caller&latency=120000&peerlatency=120000&transtype=live",
        help="OBS listener URL; FFmpeg sends MPEG-TS to this SRT endpoint",
    )
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--gop-seconds", type=int, default=1)
    parser.add_argument("--video-bitrate", default="3500k")
    parser.add_argument("--audio-bitrate", default="96k")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    if bool(args.cert_file) != bool(args.key_file):
        parser.error("--cert-file and --key-file must be provided together")
    if not args.srt_url.startswith("srt://"):
        parser.error("--srt-url must use the srt:// protocol")
    if args.fps < 1 or args.gop_seconds < 1:
        parser.error("--fps and --gop-seconds must be positive")
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    tls: ssl.SSLContext | None = None
    if args.cert_file:
        tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        tls.load_cert_chain(args.cert_file, args.key_file)
    else:
        LOG.warning("https_disabled browser camera and microphone access will fail on most phones")

    engine = InferenceEngine(args.source, args.execution_provider)
    config = StreamConfig(
        ffmpeg=args.ffmpeg,
        srt_url=args.srt_url,
        fps=args.fps,
        video_bitrate=args.video_bitrate,
        audio_bitrate=args.audio_bitrate,
        gop_seconds=args.gop_seconds,
    )
    app = create_app(config, engine)
    web.run_app(app, host=args.host, port=args.port, ssl_context=tls, handle_signals=True)


if __name__ == "__main__":
    main()
