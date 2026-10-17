"""OBS-friendly text exports for the currently playing song and queue."""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Dict, Iterable, List

from django.conf import settings as conf

from core.musiq import song_utils
from core.safe_files import atomic_write_text, retry_file_operation, safe_unlink

LOGGER = logging.getLogger(__name__)
MAX_QUEUE_FILES = 99
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_stop = threading.Event()
EMPTY_QUEUE_LINES = ["Queue empty", "Add music using our website: furatic.xyz"]


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _write_lines(path: Path, lines: Iterable[str], *, attempts: int = 8) -> None:
    payload = "\n".join(_stringify(line) for line in lines) + "\n"
    atomic_write_text(path, payload, encoding="utf-8", attempts=attempts, logger=LOGGER)


def _song_lines(position, duration, title, artist, votes) -> List[str]:
    lines = [position, duration, title]
    artist = song_utils.display_artist(artist, title)
    if artist and artist.strip():
        lines.append(artist)
    lines.append(_stringify(votes or 0) + (" vote" if votes == 1 else " votes"))
    return lines


def start() -> None:
    """Keep the timestamp independent of playback, downloads and queue exports."""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_tick_loop, name="obs-current-song", daemon=True)
        _worker.start()


def stop() -> None:
    _stop.set()


def _tick_loop() -> None:
    from django.db import close_old_connections, connections
    from django.utils import timezone
    from core import models, redis
    from core.musiq import audio_tail
    from core.settings import storage

    deadline = time.monotonic()
    try:
        while not _stop.is_set():
            success = False
            try:
                close_old_connections()
                song = models.CurrentSong.objects.first()
                duration = audio_tail.effective_duration_for_song(song) if song else 0.0
                position = 0.0
                if song:
                    now = song.last_paused if storage.get("paused") else timezone.now()
                    position = (now - song.created).total_seconds()
                if redis.get("alarm_playing"):
                    from core.musiq import musiq
                    song = SimpleNamespace(**musiq.get_alarm_metadata())
                    duration, position = song.duration, 0.0
                elif redis.get("backup_playing"):
                    song = SimpleNamespace(title="Backup Stream", artist="", votes=0)
                    duration, position = 60 * 60 * 24, 0.0
                success = write_current_song_tick(
                    song, position, duration,
                    queue_empty=not song and not models.QueuedSong.objects.exists(),
                )
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("failed to refresh OBS current song")
                connections.close_all()
            now = time.monotonic()
            # Keep a steady cadence; after a slow write, immediately refresh
            # from current state instead of replaying a backlog of old seconds.
            deadline = max(deadline + 1.0, now) if success else now + 0.25
            _stop.wait(max(0.0, deadline - now))
    finally:
        connections.close_all()


def _current_position_text(current_song: Dict[str, Any] | None, progress: Any) -> str:
    if not current_song:
        return ""
    try:
        duration = float(current_song.get("duration") or 0)
        progress_percent = float(progress or 0)
    except (TypeError, ValueError):
        return ""
    current_position = max(0.0, min(duration, duration * progress_percent / 100.0))
    return song_utils.format_seconds(current_position)

def write_current_song_tick(
    current_song: Any,
    progress_seconds: float,
    effective_duration: float,
    *,
    queue_empty: bool = False,
) -> bool:
    """Write only songcurrent.txt for once-per-second OBS timestamp updates."""
    try:
        output_dir = Path(conf.FURATIC_OBS_OUTPUT_DIR).expanduser()
        retry_file_operation(
            lambda: output_dir.mkdir(parents=True, exist_ok=True),
            description=f"create OBS export directory {output_dir}",
            logger=LOGGER,
            attempts=1,
        )

        progress_seconds = max(0.0, min(float(effective_duration or 0.0), float(progress_seconds or 0.0)))
        effective_duration = max(0.0, float(effective_duration or 0.0))

        current_lines = _song_lines(
            song_utils.format_seconds(progress_seconds),
            song_utils.format_seconds(effective_duration),
            getattr(current_song, "title", "") or "",
            getattr(current_song, "artist", ""),
            getattr(current_song, "votes", 0),
        ) if current_song else (EMPTY_QUEUE_LINES if queue_empty else ["", "", "", "", ""])
        # Retry soon with a fresh timestamp rather than blocking for seconds
        # in Windows sharing-violation backoff with a stale payload.
        _write_lines(output_dir / "songcurrent.txt", current_lines, attempts=1)
        return True
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("failed to write OBS current song tick")
        return False

def write_from_state(state: Dict[str, Any]) -> None:
    """Write songcurrent.txt and songqueue*.txt files for OBS / overlays."""
    try:
        musiq_state = state.get("musiq") or {}
        current_song = musiq_state.get("currentSong")
        progress = musiq_state.get("progress")
        queue = list(musiq_state.get("songQueue") or [])[:MAX_QUEUE_FILES]

        output_dir = Path(conf.FURATIC_OBS_OUTPUT_DIR).expanduser()
        retry_file_operation(
            lambda: output_dir.mkdir(parents=True, exist_ok=True),
            description=f"create OBS export directory {output_dir}",
            logger=LOGGER,
        )

        if current_song:
            current_lines = _song_lines(
                _current_position_text(current_song, progress),
                current_song.get("durationFormatted")
                or song_utils.format_seconds(current_song.get("duration") or 0),
                current_song.get("title") or current_song.get("name") or "",
                current_song.get("artist"),
                current_song.get("votes"),
            )
        else:
            current_lines = ["", "", "", "", ""] if queue else EMPTY_QUEUE_LINES
        # Only one writer owns the live timestamp. Concurrent state snapshots
        # must not replace a newer tick with an older progress value.
        if _worker is None or not _worker.is_alive():
            _write_lines(output_dir / "songcurrent.txt", current_lines)
        _write_lines(
            output_dir / "songgenre.txt",
            [(current_song or {}).get("genre") or ""],
        )
        _write_lines(
            output_dir / "songartwork.txt",
            [(current_song or {}).get("artworkUrl") or ""],
        )

        for index, song in enumerate(queue, start=1):
            queue_lines = [
                song.get("title") or song.get("name") or "",
                song_utils.display_artist(song.get("artist"), song.get("title") or song.get("name")),
                _stringify(song.get("votes") or 0) + (" vote" if song.get("votes") == 1 else " votes"),
                song.get("durationFormatted")
                or song_utils.format_seconds(song.get("duration") or 0),
            ]
            _write_lines(output_dir / f"songqueue{index}.txt", queue_lines)

        for stale_path in output_dir.glob("songqueue*.txt"):
            suffix = stale_path.stem.replace("songqueue", "")
            if not suffix.isdigit():
                continue
            index = int(suffix)
            if index < 1 or index > len(queue):
                safe_unlink(stale_path, missing_ok=True, logger=LOGGER)
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("failed to write OBS export files")
