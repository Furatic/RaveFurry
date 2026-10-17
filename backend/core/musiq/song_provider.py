"""This module contains the base class of all song providers."""
import datetime
import logging
from typing import Optional, Type, List, Dict, Callable, Tuple

from django.db import transaction
from django.db.models.expressions import F
from django.http.response import HttpResponse
from django.utils import timezone

from core.models import (
    ArchivedQuery,
    ArchivedSong,
    QueuedSong,
    RequestLog,
    CurrentSong,
    PlayLog,
    RecentPlay,
)
from core.musiq import audio_tail, musiq, playback, song_utils
from core.musiq.music_provider import MusicProvider, WrongUrlError, ProviderError
from core.musiq.song_utils import Metadata
from core.settings import storage

import time
from django.db import OperationalError


class SongProvider(MusicProvider):
    """The base class for all single song providers."""

    @staticmethod
    def get_id_from_external_url(url: str) -> str:
        """Constructs and returns the external id based on the given url."""
        raise NotImplementedError()

    @staticmethod
    def create(
        query: Optional[str] = None,
        key: Optional[int] = None,
        external_url: Optional[str] = None,
    ) -> "SongProvider":
        """Factory method to create a song provider.
        Either (query and key) or external url need to be specified.
        Detects the type of provider needed and returns one of corresponding type."""
        if key is not None:
            if query is None:
                logging.error(
                    "archived song requested but no query given (key %s)", key
                )
                raise ValueError()
            try:
                archived_song = ArchivedSong.objects.get(id=key)
            except ArchivedSong.DoesNotExist as error:
                logging.error("archived song requested for nonexistent key %s", key)
                raise ValueError() from error
            external_url = archived_song.url
        if external_url is None:
            raise ValueError(
                "external_url was provided and could not be inferred from remaining attributes."
            )
        provider_class: Optional[Type[SongProvider]] = None
        url_type = song_utils.determine_url_type(external_url)
        if url_type == "local":
            from core.musiq.local import LocalSongProvider

            provider_class = LocalSongProvider
        elif storage.get("youtube_enabled") and url_type == "youtube":
            from core.musiq.youtube import YoutubeSongProvider

            provider_class = YoutubeSongProvider
        elif storage.get("spotify_enabled") and url_type == "spotify":
            from core.musiq.spotify import SpotifySongProvider

            provider_class = SpotifySongProvider
        elif storage.get("soundcloud_enabled") and url_type == "soundcloud":
            from core.musiq.soundcloud import SoundcloudSongProvider

            provider_class = SoundcloudSongProvider
        elif storage.get("jamendo_enabled") and url_type == "jamendo":
            from core.musiq.jamendo import JamendoSongProvider

            provider_class = JamendoSongProvider
        if not provider_class:
            raise ProviderError(f"No provider for given song: {external_url}")
        if not query and external_url:
            query = external_url
        provider = provider_class(query, key)
        return provider

    def __init__(self, query: Optional[str], key: Optional[int]) -> None:
        super().__init__(query, key)
        self.id = self.extract_id()
        self.ok_message = "song queued"
        self.queued_song: Optional[QueuedSong] = None
        self.metadata: "Metadata" = {}

        if query:
            url_type = song_utils.determine_url_type(query)
            if url_type not in (self.type, "unknown"):
                raise WrongUrlError(
                    f"Tried to create a {self.type} provider with: {query}"
                )

    def get_internal_url(self) -> str:
        """Returns the internal url based on this object's id."""
        raise NotImplementedError()

    def get_external_url(self) -> str:
        """Returns the external url based on this object's id."""
        raise NotImplementedError()

    def extract_id(self) -> Optional[str]:
        """Tries to extract the id from the given query.
        Returns the id if possible, otherwise None"""
        if self.key is not None:
            try:
                archived_song = ArchivedSong.objects.get(id=self.key)
                return self.__class__.get_id_from_external_url(archived_song.url)
            except (ArchivedSong.DoesNotExist, KeyError, ValueError):
                return None

        if self.query is not None:
            url_type = song_utils.determine_url_type(self.query)

            provider_class: Optional[Type[SongProvider]] = None

            if url_type == "local":
                from core.musiq.local import LocalSongProvider

                provider_class = LocalSongProvider
            if storage.get("youtube_enabled") and url_type == "youtube":
                from core.musiq.youtube import YoutubeSongProvider

                provider_class = YoutubeSongProvider

            if storage.get("spotify_enabled") and url_type == "spotify":
                from core.musiq.spotify import SpotifySongProvider

                provider_class = SpotifySongProvider

            if storage.get("soundcloud_enabled") and url_type == "soundcloud":
                from core.musiq.soundcloud import SoundcloudSongProvider

                provider_class = SoundcloudSongProvider
            if storage.get("jamendo_enabled") and url_type == "jamendo":
                from core.musiq.jamendo import JamendoSongProvider

                provider_class = JamendoSongProvider

            if provider_class is not None:
                try:
                    return provider_class.get_id_from_external_url(self.query)
                except (KeyError, ValueError):
                    return None

            try:
                archived_song = ArchivedSong.objects.get(url=self.query)
                return self.__class__.get_id_from_external_url(archived_song.url)
            except (ArchivedSong.DoesNotExist, KeyError, ValueError):
                return None

        logging.error("Can not extract id because neither key nor query are known")
        return None

    def enqueue_placeholder(
        self,
        manually_requested,
        requester_ip: str = "",
        requester_session_key: str = "",
    ) -> None:
        metadata: Metadata = {
            "artist": "",
            "title": self.query or self.get_external_url(),
            "duration": -1,
            "internal_url": None,
            "external_url": self.get_external_url(),
            "stream_url": None,
            "cached": False,
        }
        initial_votes = 1 if manually_requested else 0
        self.queued_song = playback.queue.enqueue(
            metadata,
            manually_requested,
            votes=initial_votes,
            enqueue_first=storage.get("enqueue_first"),
            requester_ip=requester_ip,
            requester_session_key=requester_session_key,
        )

    def remove_placeholder(self) -> None:
        assert self.queued_song

        queue_key = self.queued_song.id
        requester_ip = getattr(self.queued_song, "requester_ip", "") or ""
        requester_session_key = getattr(self.queued_song, "requester_session_key", "") or ""

        try:
            from core import user_manager  # pylint: disable=import-outside-toplevel

            user_manager.release_queue_slot_for_song(queue_key)
            user_manager.clear_request_cooldown(requester_ip, requester_session_key)
        except Exception:  # pylint: disable=broad-except
            logging.exception(
                "failed to release requester state for removed placeholder %s",
                queue_key,
            )

        try:
            playback.queue.remove(queue_key)
        except QueuedSong.DoesNotExist:
            pass

    def check_cached(self) -> bool:
        return False

    def check_not_too_large(self, size: Optional[float]) -> bool:
        """Returns whether the given file size is small enough to be played."""
        max_size = storage.get("max_download_size") * 1024 * 1024
        if (
            max_size != 0
            and not self.check_cached()
            and (size is not None and size > max_size)
        ):
            self.error = "Song too large"
            return False
        return True

    def check_not_too_long(self, duration: Optional[float]) -> bool:
        """Return whether the given duration is allowed by the moderator setting."""

        max_duration = float(storage.get("max_song_duration_seconds"))
        if max_duration <= 0 or duration is None:
            return True

        if duration > max_duration:
            self.error = (
                "Sorry, songs over "
                f"{song_utils.format_seconds(max_duration)} long are not allowed."
            )
            return False

        return True

    def check_available(self) -> bool:
        raise NotImplementedError()

    def make_available(self) -> bool:
        return True

    def get_local_metadata(self, path: str) -> "Metadata":
        """Collects Metadata about this song from the local system,
        either from the database if available or the filesystem.
        Used by localdrive and youtube."""
        if not self.id:
            raise ValueError()

        get_metadata_from_fs = False
        try:
            # Try to read the metadata from the database
            archived_song = ArchivedSong.objects.get(url=self.get_external_url())
            metadata = archived_song.get_metadata()
            if not metadata["cached"]:
                get_metadata_from_fs = True
        except ArchivedSong.DoesNotExist:
            get_metadata_from_fs = True
        # If this is not possible, or the metadata is not cached, read it from the file system
        if get_metadata_from_fs:
            metadata = song_utils.get_metadata(path)

        metadata["internal_url"] = self.get_internal_url()
        metadata["external_url"] = self.get_external_url()
        metadata["stream_url"] = None
        if not metadata["title"]:
            if not metadata["external_url"]:
                raise ValueError
            metadata["title"] = metadata["external_url"]

        return metadata

    def first_unfiltered_item(
        self, items: List[Dict], get_title_and_artist: Callable[[Dict], Tuple[str, str]]
    ) -> Optional[Dict]:
        """Returns the first item in the given list that is not filtered."""
        for item in items:
            artist, title = get_title_and_artist(item)
            if song_utils.is_forbidden(artist) or song_utils.is_forbidden(title):
                continue
            return item
        self.error = "All results filtered"
        return None

    def gather_metadata(self) -> bool:
        """Fetches metadata for this song, from the internet if necessary.
        Returns True if successful, False otherwise."""
        raise NotImplementedError

    def get_metadata(self) -> "Metadata":
        """Returns a dictionary of this song's metadata."""
        if not self.metadata:
            self.gather_metadata()
        return self.metadata

    def was_requested_before(self) -> bool:
        try:
            archived_song = ArchivedSong.objects.get(url=self.get_external_url())
            if archived_song.counter > 0:
                return True
        except ArchivedSong.DoesNotExist:
            pass
        return False

    def on_cooldown(self) -> bool:
        try:
            current_song = CurrentSong.objects.get()
        except (CurrentSong.DoesNotExist, CurrentSong.MultipleObjectsReturned):
            current_song = None
        if playback.queue.filter(external_url=self.get_external_url()).count() > 0 or (
            current_song is not None
            and current_song.external_url == self.get_external_url()
        ):
            self.error = "Song already in queue"
            return True
        latest_log = (
            PlayLog.objects.filter(song__url=self.get_external_url())
            .order_by("-created")
            .first()
        )
        recent_hour = RecentPlay.objects.filter(
            song_url=self.get_external_url(), created__gte=timezone.now() - datetime.timedelta(hours=1)
        ).count()
        recent_day = RecentPlay.objects.filter(
            song_url=self.get_external_url(), created__gte=timezone.now() - datetime.timedelta(hours=24)
        ).count()
        if recent_hour >= 1:
            self.error = "This song was played in the last hour and can be queued again once one hour has passed."
            return True
        if recent_day >= 2:
            self.error = "This song has already played twice in the last 24 hours and cannot be queued again yet."
            return True
        cooldown = datetime.timedelta(hours=storage.get("song_cooldown"))
        if latest_log is not None and timezone.now() - latest_log.created < cooldown:
            self.error = "Song was played recently"
            return True
        return False

    def persist(self, session_key: str, archive: bool = True) -> None:
        metadata = self.get_metadata()
    
        last_error = None
        for attempt in range(5):
            try:
                # Increase counter of song/playlist
                with transaction.atomic():
                    queryset = ArchivedSong.objects.filter(url=metadata["external_url"])
                    if queryset.count() == 0:
                        initial_counter = 1 if archive else 0
                        assert metadata["external_url"]
                        archived_song = ArchivedSong.objects.create(
                            url=metadata["external_url"],
                            artist=metadata["artist"],
                            title=metadata["title"],
                            duration=metadata["duration"],
                            counter=initial_counter,
                            cached=metadata["cached"],
                        )
                    else:
                        if archive:
                            queryset.update(counter=F("counter") + 1)
                        archived_song = queryset.get()
    
                    if archive:
                        ArchivedQuery.objects.get_or_create(
                            song=archived_song, query=self.query
                        )
                break
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.2 * (attempt + 1))
        else:
            raise last_error
    
        if storage.get("logging_enabled") and session_key:
            RequestLog.objects.create(song=archived_song, session_key=session_key)

    def enqueue(self) -> None:
        assert self.queued_song
        if not playback.queue.filter(id=self.queued_song.id).exists():
            # this song was already deleted, do not enqueue
            return

        metadata = self.get_metadata()

        if not self.check_not_too_long(metadata.get("duration")):
            logging.info(
                "song rejected by max duration setting: query=%r url=%r duration=%r",
                self.query,
                metadata.get("external_url"),
                metadata.get("duration"),
            )
            self.remove_placeholder()
            musiq.update_state()
            return

        try:
            audio_tail.analyze_and_cache(
                str(metadata.get("internal_url") or ""),
                float(metadata.get("duration") or 0.0),
            )
        except Exception:  # pylint: disable=broad-except
            logging.exception(
                "failed to analyze quiet tail for %r",
                metadata.get("external_url"),
            )

        assert metadata["external_url"]
        self.queued_song.artist = metadata["artist"]
        self.queued_song.title = metadata["title"]
        self.queued_song.duration = metadata["duration"]
        self.queued_song.internal_url = metadata["internal_url"]
        self.queued_song.external_url = metadata["external_url"]
        self.queued_song.stream_url = metadata["stream_url"]
        self.queued_song.artwork_url = metadata.get("artwork_url", "")
        self.queued_song.genre = metadata.get("genre", "")
        current_review_status = playback.queue.filter(
            id=self.queued_song.id,
        ).values_list("review_status", flat=True).first()
        self.queued_song.review_status = (
            "pending" if current_review_status == "pending" else "analyzing"
        )
        # make sure not to overwrite the index as it may have changed in the meantime
        self.queued_song.save(
            update_fields=[
                "artist",
                "title",
                "duration",
                "internal_url",
                "external_url",
                "stream_url",
                "artwork_url",
                "genre",
                "review_status",
            ]
        )
        from core import audit_log

        audit_log.update_song_title(
            self.queued_song.id,
            self.queued_song.displayname(),
        )
        from core.song_analysis import analyze_song

        analyze_song.delay(self.queued_song.id)

        musiq.update_state()
        playback.queue_changed.set()

    def get_suggestion(self) -> str:
        """Returns the external url of a suggested song based on this one."""
        raise NotImplementedError()

    def request_radio(self, session_key) -> HttpResponse:
        """Enqueues a playlist of songs based on this one."""
        raise NotImplementedError()
