"""Runtime restart coordination for the local Windows web child."""

from __future__ import annotations

import logging

from core import redis, site_mode
from core.settings import storage

logger = logging.getLogger(__name__)

RESTART_REQUEST_KEY = "web_restart_requested"
RESTART_REASON_BETWEEN_SONGS = "between_songs"


def restart_requested() -> bool:
    try:
        return bool(redis.connection.get(RESTART_REQUEST_KEY))
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to read restart request: %s", error)
        return False


def request_between_songs_restart() -> None:
    # TTL prevents a stuck restart request from blocking playback forever
    # if the launcher is not supervising for some reason.
    redis.put(RESTART_REQUEST_KEY, RESTART_REASON_BETWEEN_SONGS, expire=60)


def clear_restart_request() -> None:
    try:
        redis.connection.delete(RESTART_REQUEST_KEY)
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to clear restart request: %s", error)


def record_completed_song() -> bool:
    mode = site_mode.get_mode()
    if mode not in {site_mode.EVENT_MODE, site_mode.CLOSING_MODE}:
        return False

    interval = int(storage.get("maintenance_restart_song_interval"))
    if interval == 20:
        # Existing installations may restore the old interval from DB or disk.
        interval = 70
        storage.put("maintenance_restart_song_interval", interval)
    if interval <= 0:
        return False

    count = int(storage.get("songs_since_maintenance_restart")) + 1

    if count >= interval:
        storage.put("songs_since_maintenance_restart", 0)
        logger.info("planned web restart after %s completed songs", interval)
        return True

    storage.put("songs_since_maintenance_restart", count)
    return False
