"""OBS-friendly text exports for the currently playing song and queue."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List

from django.conf import settings as conf

from core.musiq import song_utils
from core.safe_files import atomic_write_text, retry_file_operation, safe_unlink

LOGGER = logging.getLogger(__name__)
MAX_QUEUE_FILES = 99


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _write_lines(path: Path, lines: Iterable[str]) -> None:
    payload = "\n".join(_stringify(line) for line in lines) + "\n"
    atomic_write_text(path, payload, encoding="utf-8", logger=LOGGER)


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
) -> None:
    """Write only songcurrent.txt for once-per-second OBS timestamp updates."""
    try:
        output_dir = Path(conf.FURATIC_OBS_OUTPUT_DIR).expanduser()
        retry_file_operation(
            lambda: output_dir.mkdir(parents=True, exist_ok=True),
            description=f"create OBS export directory {output_dir}",
            logger=LOGGER,
        )

        progress_seconds = max(0.0, min(float(effective_duration or 0.0), float(progress_seconds or 0.0)))
        effective_duration = max(0.0, float(effective_duration or 0.0))

        current_lines = [
            song_utils.format_seconds(progress_seconds),
            song_utils.format_seconds(effective_duration),
            getattr(current_song, "title", "") or "",
            song_utils.display_artist(getattr(current_song, "artist", ""), getattr(current_song, "title", "")),
            _stringify(getattr(current_song, "votes", 0) or 0) + (" vote" if getattr(current_song, "votes", 0) == 1 else " votes"),
        ]
        _write_lines(output_dir / "songcurrent.txt", current_lines)
        if current_song:
            _write_lines(
                output_dir / "songgenre.txt",
                [getattr(current_song, "genre", "") or ""],
            )
            _write_lines(
                output_dir / "songartwork.txt",
                [getattr(current_song, "artwork_url", "") or ""],
            )
    except Exception:  # pylint: disable=broad-except
        LOGGER.exception("failed to write OBS current song tick")

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
            current_lines = [
                _current_position_text(current_song, progress),
                current_song.get("durationFormatted")
                or song_utils.format_seconds(current_song.get("duration") or 0),
                current_song.get("title") or current_song.get("name") or "",
                song_utils.display_artist(current_song.get("artist"), current_song.get("title") or current_song.get("name")),
                _stringify(current_song.get("votes") or 0) + (" vote" if current_song.get("votes") == 1 else " votes"),
            ]
        else:
            current_lines = ["", "", "", "", ""]
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
