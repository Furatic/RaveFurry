"""This module contains the base class of all music providers."""


from __future__ import annotations

import logging
from threading import Timer
from typing import Optional

from core import models, redis, ui_notifications
from core.musiq import musiq, playback, player
from core.settings import storage
from core.tasks import app, CELERY_ACTIVE


def _stop_idle_backup_stream_after_failure() -> None:
    """Do not leave the backup stream running after a failed manual request."""
    if not redis.get("backup_playing"):
        return
    if models.CurrentSong.objects.exists():
        return
    if playback.queue.confirmed().exists():
        return

    try:
        player.pause()
    except Exception:  # pylint: disable=broad-except
        logging.exception("failed to stop backup stream after fetch failure")
    redis.put("backup_playing", False)


class ProviderError(Exception):
    """An error to indicate that an error occurred while providing music."""

class WrongUrlError(Exception):
    """An error to indicate that a provider was called
    with a url that belongs to a different service."""


class MusicProvider:
    """The base class for all music providers.
    Provides abstract function declarations."""

    def __init__(self, query: Optional[str], key: Optional[int]) -> None:
        self.query = query
        self.key = key
        if not hasattr(self, "type"):
            # the type should already have been set by the base class
            self.type = "unknown"
            assert False
        self.id: Optional[str] = None
        self.ok_message = "ok"
        self.error = "error"

    def check_cached(self) -> bool:
        """Returns whether this resource is available on disk."""
        raise NotImplementedError()

    def check_available(self) -> bool:
        """Returns whether this resource is available online."""
        raise NotImplementedError()

    def enqueue_placeholder(
        self,
        manually_requested,
        requester_ip: str = "",
        requester_session_key: str = "",
    ) -> None:
        """Enqueues a placeholder if applicable. Playlists have no placeholder, only songs do.
        Used to identify this resource in the client after a request.
        """
        raise NotImplementedError()

    def remove_placeholder(self) -> None:
        """Removes the placeholder in the queue that represents this resource.
        Called if there was an error and this element needs to be removed from the queue.
        """
        raise NotImplementedError()

    def make_available(self) -> bool:
        """Makes this resource available for playback.
        If possible, downloads it to disk.
        If this takes a long time, calls update_state so the placeholder is visible.
        Returns False if an error occured, True otherwise."""
        raise NotImplementedError()

    def was_requested_before(self) -> bool:
        """Returns whether this resource has been manually requested at least once."""
        raise NotImplementedError()

    def on_cooldown(self) -> bool:
        """Returns whether this resource is currently on cooldown and may not be requested.
        Sets the provider error."""
        raise NotImplementedError()

    def persist(self, session_key: str, archive: bool = True) -> None:
        """Updates the database.
        Creates an archived entry or updates it.
        Also handles logging to database."""
        raise NotImplementedError()

    def enqueue(self) -> None:
        """Updates the placeholder in the song queue with the actual data."""
        raise NotImplementedError()

    def request(
        self,
        session_key: str,
        archive: bool = True,
        manually_requested: bool = True,
        requester_ip: str = "",
        defer_enqueue: bool = False,
    ) -> None:
        """Tries to request this resource.
        Uses the local cache if possible, otherwise tries to retrieve it online."""

        self.error = ""

        if 0 < storage.get("max_queue_length") <= playback.queue.count():
            self.error = "Queue limit reached"
            raise ProviderError(self.error)

        enqueue_function = enqueue

        if not self.check_cached():
            if self.query is not None and storage.get("additional_keywords"):
                # add the additional keywords from the settings before checking
                self.query += " " + storage.get("additional_keywords")
            if not self.check_available():
                if not (defer_enqueue and self.type == "youtube" and self.id and getattr(self, "download_rejected", False)):
                    raise ProviderError(self.error)
                self.error = ""  # Retryable YouTube metadata rejection follows the download/retry path.

            # overwrite the enqueue function and make the resource available before calling it
            enqueue_function = fetch_enqueue

        if storage.get("new_music_only"):
            if self.was_requested_before():
                self.error = "Only new music is allowed!"
                raise ProviderError(self.error)

        # FURATIC's one-hour / twice-per-day replay limits are mandatory for
        # user submissions even when the older configurable setting is zero.
        # Preserve the prior behavior for automatic entries when operators
        # explicitly enable that legacy cooldown.
        if (
            manually_requested or storage.get("song_cooldown") != 0
        ) and self.on_cooldown():
            raise ProviderError(self.error)

        self.enqueue_placeholder(
            manually_requested,
            requester_ip=requester_ip,
            requester_session_key=session_key if manually_requested else "",
        )

        if defer_enqueue:
            self._deferred_enqueue = (enqueue_function, session_key, archive)
        else:
            enqueue_function.delay(self, session_key, archive)

    def start_deferred_enqueue(self) -> None:
        """Start background work after caller-owned queue metadata is committed."""
        deferred = getattr(self, "_deferred_enqueue", None)
        if deferred is None:
            return
        enqueue_function, session_key, archive = deferred
        del self._deferred_enqueue
        enqueue_function.delay(self, session_key, archive)


@app.task
def enqueue(provider: MusicProvider, session_key: str, archive: bool) -> None:
    """Enqueue the music managed by the given provider."""
    try:
        _finish_enqueue(provider, session_key, archive)
    except Exception:
        if not getattr(provider, "submission_token", ""):
            raise
        logging.exception("enqueue failed for submitted song %r", provider.query)
        _submission_failure(provider, session_key, archive, retry=False)


def _notify_submission(provider: MusicProvider, message: str, error: bool = False) -> None:
    token = getattr(provider, "submission_token", "")
    if token:
        ui_notifications.emit(
            "song_request_error" if error else "song_request_ready",
            getattr(provider, "submission_query", "") or "Your song",
            message,
            level="error" if error else "success",
            recipient_token=token,
            request_key=provider.queued_song.id,
        )


def _finish_enqueue(provider: MusicProvider, session_key: str, archive: bool) -> None:
    submitted = bool(getattr(provider, "submission_token", ""))
    if submitted and not playback.queue.filter(id=provider.queued_song.id).exists():
        return  # A removed request must never reappear when background work finishes.
    if not submitted or not getattr(provider, "_submission_persisted", False):
        provider.persist(session_key, archive=archive)
        if submitted:
            provider._submission_persisted = True
    provider.enqueue()
    if submitted:
        if _submission_ready(provider):
            _notify_submission(provider, provider.submission_message)
        elif playback.queue.filter(id=provider.queued_song.id).exists():
            raise ProviderError("The song did not become ready.")
        elif provider.error:
            _notify_submission(provider, provider.error, error=True)


def _submission_ready(provider: MusicProvider) -> bool:
    return (
        playback.queue.filter(id=provider.queued_song.id).exclude(internal_url=None).exclude(internal_url="").exists()
        or models.CurrentSong.objects.filter(occurrence_id=provider.queued_song.occurrence_id).exists()
    )


def _submission_failure(provider: MusicProvider, session_key: str, archive: bool, retry: bool) -> None:
    submitted = bool(getattr(provider, "submission_token", ""))
    if submitted and _submission_ready(provider):
        _notify_submission(provider, provider.submission_message)
        return
    if submitted and not playback.queue.filter(id=provider.queued_song.id).exists():
        return
    if submitted and provider.type == "youtube" and not retry:
        provider.download_incomplete = True
        _notify_submission(provider, "YouTube rejected our request to download that song, trying again in 30 seconds.", error=True)
        if CELERY_ACTIVE:
            fetch_enqueue.apply_async(args=(provider, session_key, archive, True), countdown=30)
        else:
            # Basic/Windows mode has no Celery scheduler; do not occupy a worker while waiting.
            timer = Timer(30, lambda: fetch_enqueue.delay(provider, session_key, archive, True))
            timer.daemon = True
            timer.start()
        return
    if submitted:
        message = "YouTube won't let us download this song right now :/" if provider.type == "youtube" else (provider.error or "This song could not be downloaded.")
        _notify_submission(provider, message, error=True)
    try:
        provider.remove_placeholder()
    except Exception:
        logging.exception("failed to remove placeholder after fetch_enqueue error")
    _stop_idle_backup_stream_after_failure()
    musiq.update_state()


@app.task
def fetch_enqueue(provider: MusicProvider, session_key: str, archive: bool, retry: bool = False) -> None:
    """Fetch and enqueue the music managed by the given provider."""
    try:
        if getattr(provider, "submission_token", "") and not playback.queue.filter(id=provider.queued_song.id).exists():
            return
        if getattr(provider, "submission_token", ""):
            provider.error = ""
        if not provider.make_available():
            _submission_failure(provider, session_key, archive, retry)
            return

        _finish_enqueue(provider, session_key, archive)
    except Exception:
        logging.exception("fetch_enqueue failed for provider query=%r key=%r", provider.query, provider.key)
        _submission_failure(provider, session_key, archive, retry)
