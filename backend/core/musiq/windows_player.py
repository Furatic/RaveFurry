"""Windows native playback backend using python-vlc."""

from __future__ import annotations

import os
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from core import redis
from core.musiq import player
from core.musiq.playback import PlaybackError


def _find_vlc_dir() -> Optional[Path]:
    if os.name != "nt":
        return None

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "VideoLAN" / "VLC",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "VideoLAN" / "VLC",
    ]

    for candidate in candidates:
        if (candidate / "libvlc.dll").exists():
            os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(str(candidate))
            return candidate

    return None


_VLC_DIR = _find_vlc_dir()

try:
    import vlc  # python-vlc
except ImportError:
    vlc = None


_INSTANCE: Optional["WindowsPlayer"] = None
logger = logging.getLogger(__name__)


def recovery_would_interrupt_playback() -> bool:
    """Defer recovery while the existing native player can still produce audio.

    Called only when runtime health has failed, not during normal health polls.
    Do not create another player while inspecting the existing one.
    """
    if _INSTANCE is None:
        return False
    try:
        state = _INSTANCE.media_player.get_state()
    except Exception:  # pylint: disable=broad-except
        logger.exception("cannot inspect VLC state; deferring web recovery")
        return True
    return state in (vlc.State.Opening, vlc.State.Buffering, vlc.State.Playing)


def _get_instance() -> "WindowsPlayer":
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = WindowsPlayer()
    return _INSTANCE

def _normalize_media_source(uri: str) -> str:
    """Normalize file URIs/paths so VLC gets a valid Windows file URI."""
    if os.name != "nt":
        return uri

    if uri.startswith("file://"):
        parsed = urlparse(uri)

        # Handle malformed values like:
        # file://C%3A%5CUsers%5Cname%5CMusic%5Csong.m4a
        if parsed.netloc and not parsed.path:
            raw_path = unquote(parsed.netloc)
        else:
            raw_path = unquote(parsed.path)
            if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
                raw_path = raw_path[1:]

        raw_path = raw_path.replace("/", "\\")
        return Path(raw_path).resolve().as_uri()

    if re.match(r"^[A-Za-z]:[\\/]", uri):
        return Path(uri).resolve().as_uri()

    return uri

class WindowsPlayer(player.Player):
    def __init__(self) -> None:
        global _INSTANCE

        if vlc is None:
            raise PlaybackError("python-vlc is not installed")

        self.instance = vlc.Instance()
        self.media_player = self.instance.media_player_new()
        self._last_state_check = 0.0
        self._last_stop_waiting = False
        self._song_playback_observed = False
        _INSTANCE = self

    def _set_media(self, uri: str) -> None:
        uri = _normalize_media_source(uri)
        media = self.instance.media_new(uri)
        if media is None:
            raise PlaybackError("VLC failed to create media")
        try:
            self.media_player.set_media(media)
        finally:
            # set_media retains its own reference. Release the caller's
            # reference even if assigning the media fails.
            media.release()

    def start_song(self, song, catch_up: Optional[float]) -> None:
        uri = song.internal_url or song.stream_url or song.external_url
        if not uri:
            raise PlaybackError("No playable URI")

        self._last_state_check = 0.0
        self._last_stop_waiting = False
        self._song_playback_observed = False

        try:
            self.media_player.stop()
        except Exception:  # pylint: disable=broad-except
            pass

        self._set_media(uri)

        try:
            volume = float(redis.get("volume"))
        except Exception:  # pylint: disable=broad-except
            volume = 1.0

        if volume <= 0:
            volume = 1.0

        self.media_player.audio_set_volume(round(max(0.0, min(1.0, volume)) * 100))

        # A recovered paused song needs VLC to open/seek the media. Keep that
        # preparation silent, including the interval before pause takes effect.
        self.media_player.audio_set_mute(bool(redis.get("paused")))

        if self.media_player.play() == -1:
            raise PlaybackError("VLC failed to start playback")

        # give VLC a moment to start
        for _ in range(20):
            state = self.media_player.get_state()
            self._song_playback_observed |= state in (vlc.State.Playing, vlc.State.Ended)
            if state not in (vlc.State.NothingSpecial, vlc.State.Opening):
                break
            time.sleep(0.1)

        if catch_up is not None and catch_up >= 0:
            self.media_player.set_time(int(catch_up))

        if redis.get("paused"):
            self.media_player.audio_set_mute(True)
            self.media_player.set_pause(1)
        else:
            self.media_player.audio_set_mute(False)

    def has_started_playback(self) -> bool:
        """Cached evidence from existing polls; this never calls VLC."""
        return self._song_playback_observed

    def should_stop_waiting(self, previous_error: bool) -> bool:
        # The playback loop calls this every 0.1s. Duration-based end detection
        # already handles normal completion, so throttle native state polling
        # while retaining checks for early stops and playback errors.
        now = time.monotonic()
        interval = 1.0 if previous_error else 5.0

        if self._last_state_check and now - self._last_state_check < interval:
            return self._last_stop_waiting

        self._last_state_check = now
        state = self.media_player.get_state()
        self._song_playback_observed |= state in (vlc.State.Playing, vlc.State.Ended)
        self._last_stop_waiting = state in (
            vlc.State.Ended,
            vlc.State.Stopped,
            vlc.State.Error,
        )
        return self._last_stop_waiting

    def play_alarm(self, interrupt: bool, alarm_path: str) -> None:
        if interrupt:
            self.media_player.stop()
        self._set_media(Path(alarm_path).resolve().as_uri())
        self.media_player.audio_set_mute(False)
        if self.media_player.play() == -1:
            raise PlaybackError("VLC failed to play alarm")

    def play_backup_stream(self) -> None:
        # current song already stores playable URLs; backup stream support can be added later
        pass

    @staticmethod
    def restart() -> None:
        inst = _get_instance()
        inst.media_player.set_time(0)
        inst.media_player.audio_set_mute(False)
        inst.media_player.play()

    @staticmethod
    def seek_backward(seek_distance: float) -> None:
        inst = _get_instance()
        pos = inst.media_player.get_time()
        inst.media_player.set_time(max(0, pos - int(seek_distance * 1000)))

    @staticmethod
    def play() -> None:
        inst = _get_instance()
        inst.media_player.audio_set_mute(False)
        inst.media_player.play()

    @staticmethod
    def pause() -> None:
        _get_instance().media_player.pause()

    @staticmethod
    def seek_forward(seek_distance: float) -> None:
        inst = _get_instance()
        pos = inst.media_player.get_time()
        inst.media_player.set_time(max(0, pos + int(seek_distance * 1000)))

    @staticmethod
    def skip() -> None:
        _get_instance().media_player.stop()

    @staticmethod
    def set_volume(volume) -> None:
        _get_instance().media_player.audio_set_volume(round(volume * 100))


def restart() -> None:
    WindowsPlayer.restart()


def seek_backward(seek_distance: float) -> None:
    WindowsPlayer.seek_backward(seek_distance)


def play() -> None:
    WindowsPlayer.play()


def pause() -> None:
    WindowsPlayer.pause()


def seek_forward(seek_distance: float) -> None:
    WindowsPlayer.seek_forward(seek_distance)


def skip() -> None:
    WindowsPlayer.skip()


def set_volume(volume) -> None:
    WindowsPlayer.set_volume(volume)

