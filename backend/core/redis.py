"""This module provides functionality to interface with Redis."""
from ast import literal_eval
import logging

from typing import Any, Dict, List, Optional, Tuple, Union, Literal

from django.conf import settings as conf
from redis import Redis
from redis.exceptions import RedisError

from core.util import strtobool

logger = logging.getLogger(__name__)

REDIS_TIMEOUT_SECONDS = 2.0

# locks:
# mopidy_lock:  controlling mopidy api accesses
# lights_lock:  ensures lights settings are not changed during device updates

# channels
# lights_settings_changed

DeviceInitialized = Literal

# values:
# maps key to default and type of value
defaults = {
    # playback
    "active_player": "fake",
    "playing": False,
    "paused": False,
    "playback_error": False,
    "playback_worker_failed": False,
    "stop_playback_loop": False,
    "web_restart_requested": "",
    "alarm_playing": False,
    "alarm_requested": False,
    "alarm_duration": 10.0,
    "last_buzzer": 0.0,
    "backup_playing": False,
    # lights
    "lights_active": False,
    "ring_initialized": False,
    "wled_initialized": False,
    "strip_initialized": False,
    "screen_initialized": False,
    "led_programs": [],
    "screen_programs": [],
    "resolutions": [],
    "current_resolution": (0, 0),
    "current_fps": 0.0,
    # settings
    "has_internet": False,
    "mopidy_available": False,
    "youtube_available": False,
    "spotify_available": False,
    "soundcloud_available": False,
    "jamendo_available": False,
    "library_scan_progress": "0 / 0 / 0",
    "bluetoothctl_active": False,
    "bluetooth_devices": [],
    "site_mode": "event",
    "operator_command": "",
    # user manager
    "active_requests": 0,
    "last_user_count_update": 0.0,
    "last_requests": {},
}

connection = Redis(
    host=conf.REDIS_HOST,
    port=conf.REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    socket_timeout=REDIS_TIMEOUT_SECONDS,
    health_check_interval=30,
)

# Keep one separate client for intentionally-blocking pub/sub waits.
# Do not give this one a short socket_timeout, or Event.wait() would stop being a wait.
blocking_connection = Redis(
    host=conf.REDIS_HOST,
    port=conf.REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    health_check_interval=30,
)


def start() -> None:
    """Initializes the module by clearing all keys."""
    try:
        connection.flushdb()
    except RedisError as error:
        logger.warning("Redis start flush failed: %s", error)

def get(key: str) -> Union[bool, int, float, str, List, Dict, Tuple]:
    """This method returns the value for the given :param key: from redis.

    Values of non-existing keys are set to their respective default value."""
    default = defaults[key]

    try:
        value = connection.get(key)
    except RedisError as error:
        logger.warning("Redis get failed for %s, using default: %s", key, error)
        return default

    if value is None:
        return default

    try:
        if type(default) is bool:
            return strtobool(value)

        if type(default) in (list, dict, tuple):
            # evaluate the stored literal
            return literal_eval(value)

        return type(default)(value)
    except (ValueError, SyntaxError, TypeError) as error:
        logger.warning(
            "Redis value for %s could not be parsed, using default: %s",
            key,
            error,
        )
        return default

def put(key: str, value: Any, expire: Optional[float] = None) -> None:
    """This method sets the value for the given :param key: to the given :param value:.

    If set, the key will expire after :param ex: seconds."""
    try:
        connection.set(key, str(value), ex=expire)
    except RedisError as error:
        logger.warning("Redis put failed for %s: %s", key, error)


class Event:
    """A class that provides basic functionality similar to threading.Event using redis."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.state_key = f"event:{self.name}:set"
        self.lock = connection.lock(f"{self.name}_lock")

    @property
    def is_set(self) -> bool:
        """Read the shared latch, including signals from another process."""
        return bool(connection.exists(self.state_key))

    def wait(self) -> None:
        """Blocks until the event is set."""
        try:
            with self.lock:
                if self.is_set:
                    return

            pubsub = blocking_connection.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(self.name)
                while True:
                    # Pub/sub alone can lose a signal between the initial
                    # check and subscribing (or while reconnecting). The shared
                    # latch keeps that signal until clear(), across processes.
                    with self.lock:
                        if self.is_set:
                            return
                    message = pubsub.get_message(timeout=1.0)
                    if message is not None and message["type"] == "message":
                        return
            finally:
                pubsub.close()
        except RedisError as error:
            logger.warning("Redis event wait failed for %s: %s", self.name, error)

    def set(self) -> None:
        """Set the event and wake up all waiting threads."""
        with self.lock:
            connection.set(self.state_key, "1")
            connection.publish(self.name, "")

    def clear(self) -> None:
        """Clear this Event, allowing threads to wait for it."""
        with self.lock:
            connection.delete(self.state_key)

