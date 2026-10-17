"""Loopback-only OBS page and metadata socket. Never imports Django or libpq."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import logging
import json
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import time

# Numerical work stays in one worker rather than spawning BLAS thread pools.
for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ[name] = "1"

from aiohttp import web, WSMsgType
from .capture import Capture

MAX_CLIENTS = 4
PAGE = Path(__file__).with_name("obs-background.html")
LOG = logging.getLogger("furatic.visualizer")


def local_request(request, port):
    """Reject foreign Hosts/Origins, including accidental reverse-proxy exposure."""
    if request.remote not in ("127.0.0.1", "::1"):
        return False
    if request.host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
        return False
    origin = request.headers.get("Origin")
    return origin in (None, "null", f"http://127.0.0.1:{port}", f"http://localhost:{port}")


class ParentGuard:
    def __init__(self, pid):
        self.pid = pid
        self.handle = None
        if pid and os.name == "nt":
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            self.kernel.OpenProcess.restype = ctypes.c_void_p
            self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            self.kernel.WaitForSingleObject.restype = ctypes.c_ulong
            self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel.CloseHandle.restype = ctypes.c_int
            # One process handle for this service's entire lifetime.
            self.handle = self.kernel.OpenProcess(0x00100000, False, pid)

    def alive(self):
        if not self.pid:
            return True
        if os.name == "nt":
            return bool(self.handle) and self.kernel.WaitForSingleObject(self.handle, 0) == 0x102
        return os.getppid() == self.pid

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def create_app(capture, port):
    clients = set()
    publishers = set()
    scene = None
    scene_at = 0.0
    scene_sequence = 0
    scene_lengths = {"motion": 4, "clouds": 16, "material": 16, "dance": 4,
                     "thumps": 4, "bass": 2, "music": 4, "energy": 4, "sizes": 4,
                     "bands": 48, "lights": 4, "edgeEnergy": 4, "edgeLights": 4,
                     "edgeMusic": 4, "edgeBands": 48}

    def snapshot():
        frame = capture.snapshot()
        if scene is not None and time.monotonic() - scene_at < .35:
            frame["scene"] = {"sequence": scene_sequence, "uniforms": scene}
        return frame
    @web.middleware
    async def only_local(request, handler):
        if not local_request(request, port):
            raise web.HTTPForbidden(text="Local OBS access only")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", f"http://127.0.0.1:{port}")
        return response

    async def page(request):
        return web.Response(body=PAGE.read_bytes(), content_type="text/html")

    async def health(request):
        alive = capture.healthy()
        return web.json_response({"service": "furatic-visualizer", "version": 1,
                                  "workerAlive": alive, "status": capture.snapshot()["status"]}, status=200 if alive else 503)

    async def features(request):
        return web.json_response(snapshot())

    async def publish_scene(request):
        nonlocal scene, scene_at, scene_sequence
        if publishers:
            raise web.HTTPServiceUnavailable(text="OBS scene publisher already connected")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16384, compress=False)
        publishers.add(ws)
        try:
            await ws.prepare(request)
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    values = json.loads(message.data)
                    if not isinstance(values, dict) or set(values) != set(scene_lengths):
                        raise ValueError("Invalid scene fields")
                    for key, length in scene_lengths.items():
                        vector = values[key]
                        if not isinstance(vector, list) or len(vector) != length:
                            raise ValueError("Invalid scene vector")
                        if any(type(value) not in (int, float) or not math.isfinite(value)
                               or abs(value) > 10000 for value in vector):
                            raise ValueError("Invalid scene value")
                    scene = values
                    scene_at = time.monotonic()
                    scene_sequence += 1
                except (ValueError, TypeError):
                    await ws.close(code=1008, message=b"Invalid scene metadata")
                    break
        finally:
            publishers.discard(ws)
            scene = None
            if not ws.closed:
                await ws.close()
        return ws

    async def socket(request):
        if len(clients) >= MAX_CLIENTS:
            raise web.HTTPServiceUnavailable(text="Visualizer client limit reached")
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1024, compress=False)
        # Reserve before the handshake's first await to enforce the client cap.
        clients.add(ws)
        sender = None
        try:
            await ws.prepare(request)
            async def send():
                try:
                    while not ws.closed:
                        frame = snapshot()
                        # A blocked OBS client is dropped, never given a growing queue.
                        await asyncio.wait_for(ws.send_json(frame), timeout=.50)
                        await asyncio.sleep(.020)
                except (ConnectionError, asyncio.TimeoutError, RuntimeError):
                    await ws.close()
            sender = asyncio.create_task(send())
            async for message in ws:
                # This is a read-only stream; commands and PCM are not accepted.
                if message.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                    await ws.close(code=1008, message=b"Read-only metadata")
                    break
        finally:
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender
            clients.discard(ws)
            if not ws.closed:
                await ws.close()
        return ws

    async def close_sockets(app):
        for ws in tuple(clients | publishers):
            with contextlib.suppress(asyncio.TimeoutError, ConnectionError):
                await asyncio.wait_for(ws.close(code=1001, message=b"Visualizer stopping"), timeout=.50)

    app = web.Application(middlewares=[only_local], client_max_size=1024)
    app.router.add_get("/", page)
    app.router.add_get("/obs-background.html", page)
    app.router.add_get("/_healthz/", health)
    app.router.add_get("/features", socket)
    app.router.add_get("/features.json", features)
    app.router.add_get("/scene", publish_scene)
    app.on_shutdown.append(close_sockets)
    return app


async def run(port, parent_pid):
    capture = Capture()
    guard = ParentGuard(parent_pid)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    stop_signals = [signal.SIGINT, signal.SIGTERM]
    if os.name == "nt":
        stop_signals.append(signal.SIGBREAK)
    for signum in stop_signals:
        try:
            loop.add_signal_handler(signum, stopping.set)
        except NotImplementedError:
            signal.signal(signum, lambda *args: loop.call_soon_threadsafe(stopping.set))
    runner = web.AppRunner(create_app(capture, port), access_log=None, shutdown_timeout=2)
    try:
        if not guard.alive():
            return
        await runner.setup()
        # This host is intentionally not configurable to 0.0.0.0.
        await web.TCPSite(runner, host="127.0.0.1", port=port).start()
        capture.start()
        while guard.alive() and not stopping.is_set():
            try:
                await asyncio.wait_for(stopping.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await runner.cleanup()
        finally:
            try:
                if capture.thread.ident is not None:
                    capture.close()
            finally:
                guard.close()


def main():
    parser = argparse.ArgumentParser(description="Local OBS audio-reactive visualizer")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--port", type=int, default=int(os.environ.get("FURATIC_VISUALIZER_PORT", "8766")))
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port from 1024 through 65535")
    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "state"))) / "Raveberry"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(log_dir / "visualizer.log", maxBytes=512000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.WARNING)
    except OSError:
        logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(run(args.port, args.parent_pid))
    except KeyboardInterrupt:
        pass
    except OSError as error:
        LOG.error("Could not bind local visualizer port: %s", error)
        raise SystemExit(73)


if __name__ == "__main__":
    main()
