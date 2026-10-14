"""Same-origin visualizer transport; audio capture remains in its existing worker."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from channels.generic.websocket import AsyncWebsocketConsumer
from django.http import FileResponse, HttpResponse
from django.views.decorators.http import require_GET
import requests


def idle_frame():
    return json.dumps({"version": 1, "status": "idle", "bands": [0] * 48,
                       "level": 0, "bass": 0, "mid": 0, "treble": 0, "activity": 0,
                       "serverTime": time.monotonic(), "kickAt": 0, "accentAt": 0,
                       "kickId": 0, "accentId": 0, "kick": 0, "accent": 0})


class FeatureFeed:
    """One bounded loopback feed per web process, shared by all page visitors."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.last_reader = 0
        self.latest = idle_frame()

    def read(self):
        with self.lock:
            self.last_reader = time.monotonic()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, daemon=True, name="website-visualizer")
                self.thread.start()
            return self.latest

    def _run(self):
        try:
            port = int(os.environ.get("FURATIC_VISUALIZER_PORT", "8766"))
            if not 1024 <= port <= 65535:
                return
            with requests.Session() as session:
                while True:
                    with self.lock:
                        if time.monotonic() - self.last_reader > 10:
                            self.thread = None
                            return
                    started = time.monotonic()
                    try:
                        response = session.get(
                            f"http://127.0.0.1:{port}/features.json",
                            timeout=(.5, .5), allow_redirects=False,
                        )
                        response.raise_for_status()
                        if len(response.content) > 32768:
                            raise ValueError("Oversized feature frame")
                        frame = response.json()
                        if not isinstance(frame, dict) or frame.get("version") != 1:
                            raise ValueError("Invalid feature frame")
                        # Capture-device names are unnecessary for public rendering.
                        frame.pop("device", None)
                        value = json.dumps(frame, separators=(",", ":"), allow_nan=False)
                    except (requests.RequestException, ValueError, TypeError):
                        value = idle_frame()
                    with self.lock:
                        self.latest = value
                    time.sleep(max(0, .020 - (time.monotonic() - started)))
        finally:
            with self.lock:
                if self.thread is threading.current_thread():
                    self.thread = None


feed = FeatureFeed()


@require_GET
def background(_request):
    page = Path(__file__).resolve().parent.parent / "visualizer" / "web-background.html"
    response = FileResponse(page.open("rb"), content_type="text/html")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def features(_request):
    # HTTP fallback also supports the Windows WSGI server and non-WebSocket proxies.
    response = HttpResponse(feed.read(), content_type="application/json")
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


class VisualizerConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        headers = dict(self.scope.get("headers", []))
        origin = headers.get(b"origin")
        if origin:
            parsed = urlsplit(origin.decode("ascii", errors="replace"))
            host = headers.get(b"host", b"").decode("ascii", errors="replace")
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != host.lower():
                await self.close(code=1008)
                return
        await self.accept()
        self.sender = asyncio.create_task(self.stream())

    async def stream(self):
        previous = None
        try:
            while True:
                value = feed.read()
                if value != previous:
                    await asyncio.wait_for(self.send(text_data=value), timeout=.5)
                    previous = value
                await asyncio.sleep(.020)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            await self.close(code=1013)

    async def disconnect(self, _code):
        sender = getattr(self, "sender", None)
        if sender:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    async def receive(self, text_data=None, bytes_data=None):
        # Website clients cannot alter audio, playback, or the canonical OBS scene.
        await self.close(code=1008)
