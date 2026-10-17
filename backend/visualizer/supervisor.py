"""Own only the visualizer child. Recovery never touches the music or website."""
from __future__ import annotations

import importlib.util
import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request


class VisualizerCompanion:
    def __init__(self, directory):
        self.directory = str(directory)
        self.stopping = threading.Event()
        self.child = None
        self.thread = None
        self.exit_code = 0
        self.port = int(os.environ.get("FURATIC_VISUALIZER_PORT", "8766"))
        # Local health checks must never pass through an environment HTTP proxy.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self):
        if os.environ.get("FURATIC_VISUALIZER", "1").lower() in ("0", "false", "off"):
            return
        needed = ["numpy", "aiohttp"] + (["pyaudiowpatch"] if os.name == "nt" else [])
        missing = [name for name in needed if importlib.util.find_spec(name) is None]
        if missing:
            self.exit_code = 2
            print("OBS visualizer skipped: missing " + ", ".join(missing)
                  + ". Re-run the updated installer to install its dependencies.", flush=True)
            return
        if not 1024 <= self.port <= 65535:
            self.exit_code = 2
            print("OBS visualizer skipped: FURATIC_VISUALIZER_PORT must be 1024..65535", flush=True)
            return
        self.thread = threading.Thread(target=self._watch, name="obs-visualizer-supervisor", daemon=True)
        self.thread.start()
        print(f"OBS background: http://127.0.0.1:{self.port}/obs-background.html", flush=True)

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=8)

    def _stop_child(self):
        child, self.child = self.child, None
        if child is None:
            return
        try:
            if os.name == "posix":
                # The child owns this new process group, including its parec
                # worker. Clean up that worker even if the service has exited.
                os.killpg(child.pid, signal.SIGTERM)
            elif child.poll() is None:
                # Address only this helper's new console process group. A
                # graceful stop lets PortAudio release the stream first.
                try:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:  # a service/pythonw parent may have no console
                    child.terminate()
            child.wait(timeout=3)
            if os.name == "posix":
                # No remaining descendant may survive a completed service exit.
                with contextlib.suppress(OSError):
                    os.killpg(child.pid, signal.SIGKILL)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    child.kill()
                child.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass

    def _healthy(self):
        try:
            with self.http.open(f"http://127.0.0.1:{self.port}/_healthz/", timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    def _watch(self):
        retry = 2.0
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                self.child = subprocess.Popen(
                    [sys.executable, "-m", "visualizer.service", "--parent-pid", str(os.getpid()), "--port", str(self.port)],
                    cwd=self.directory, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=os.name == "posix",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                bad_checks = 0
                while not self.stopping.wait(5):
                    if self.child.poll() is not None:
                        break
                    bad_checks = 0 if self._healthy() else bad_checks + 1
                    if bad_checks >= 3:
                        break
                    if time.monotonic() - started > 60:
                        retry = 2.0
                if self.child.poll() == 73:
                    # Never kill or repeatedly compete with another port owner.
                    self.exit_code = 73
                    break
                self._stop_child()
                if self.stopping.wait(retry):
                    break
                retry = min(60, retry * 2)
        except (OSError, ValueError):
            # An optional visualizer failure must never escape into the app.
            self.exit_code = 1
            pass
        finally:
            self._stop_child()
