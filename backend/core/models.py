"""Contains all database models."""
from typing import TYPE_CHECKING
import uuid

from django.db import connection, models
from django.conf import settings
from django.db.models import QuerySet

from core.musiq import song_queue, song_utils

if connection.vendor == "postgresql":
    # Only import these modules when using postgres as a database backend
    from django.contrib.postgres.indexes import (  # pylint: disable=ungrouped-imports
        GinIndex,
        OpClass,
    )

if TYPE_CHECKING:
    from core.musiq.song_utils import Metadata


# Create your models here.
class Tag(models.Model):
    """Stores hashtags."""

    text = models.CharField(max_length=100)
    active = models.BooleanField()

    def __str__(self) -> str:
        return self.text


class Counter(models.Model):
    """Stores the visitors counter. Only has one element."""

    value = models.IntegerField()

    def __str__(self) -> str:
        return str(self.value)


class ArchivedSong(models.Model):
    """Stores an archived song.
    url identifies the song uniquely in the database and on the internet (if applicable).
    """

    url = models.CharField(max_length=2000, unique=True)
    artist = models.CharField(max_length=1000)
    title = models.CharField(max_length=1000)
    duration = models.FloatField()
    counter = models.IntegerField()
    cached = models.BooleanField()

    def __str__(self) -> str:
        return self.title + " (" + self.url + "): " + str(self.counter)

    def displayname(self) -> str:
        """Formats the song using the utility method."""
        return song_utils.displayname(self.artist, self.title)

    def get_metadata(self) -> "Metadata":
        """Return the combined Metadata object of this song."""
        return {
            "artist": self.artist,
            "title": self.title,
            "duration": self.duration,
            "external_url": self.url,
            "cached": self.cached,
        }

    class Meta:
        indexes = (
            [
                GinIndex(
                    OpClass("artist", "gin_trgm_ops"),
                    name="core_archivedsong_artist_trgm",
                ),
                GinIndex(
                    OpClass("title", "gin_trgm_ops"),
                    name="core_archivedsong_title_trgm",
                ),
            ]
            if connection.vendor == "postgresql"
            else []
        )


class ArchivedPlaylist(models.Model):
    """Stores an archived playlist.
    url identifies the playlist uniquely in the database and on the internet (if applicable).
    """

    id: int
    entries: QuerySet
    list_id = models.CharField(max_length=2000)
    title = models.CharField(max_length=1000)
    created = models.DateTimeField(auto_now_add=True)
    counter = models.IntegerField()

    def __str__(self) -> str:
        return self.title + ": " + str(self.counter)


class PlaylistEntry(models.Model):
    """Stores an entry to a playlist. Connects ArchivedSong and ArchivedPlaylist."""

    playlist = models.ForeignKey(
        "ArchivedPlaylist", on_delete=models.CASCADE, related_name="entries"
    )
    index = models.IntegerField()
    url = models.CharField(max_length=2000)

    def __str__(self) -> str:
        return self.playlist.title + "[" + str(self.index) + "]: " + self.url

    class Meta:
        ordering = ["playlist", "index"]


class ArchivedQuery(models.Model):
    """Stores the queries from the musiq page and the ArchivedSong it lead to."""

    song = models.ForeignKey(
        "ArchivedSong", on_delete=models.CASCADE, related_name="queries"
    )
    query = models.CharField(max_length=1000)

    def __str__(self) -> str:
        return self.query

    class Meta:
        if connection.vendor == "postgresql":
            indexes = (
                [
                    GinIndex(
                        OpClass("query", "gin_trgm_ops"),
                        name="core_archivedquery_query_trgm",
                    )
                ]
                if connection.vendor == "postgresql"
                else []
            )


class ArchivedPlaylistQuery(models.Model):
    """Stores the queries from the musiq page and the ArchivedPlaylist it lead to."""

    playlist = models.ForeignKey(
        "ArchivedPlaylist", on_delete=models.CASCADE, related_name="queries"
    )
    query = models.CharField(max_length=1000)

    def __str__(self) -> str:
        return self.query


class QueuedSong(models.Model):
    """Stores a song in the song queue so the queue is not lost on server restart."""

    id: int
    occurrence_id = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    index = models.IntegerField()
    manually_requested = models.BooleanField()
    votes = models.IntegerField(default=0)
    internal_url = models.CharField(max_length=2000, blank=True, null=True)
    external_url = models.CharField(max_length=2000)
    stream_url = models.CharField(max_length=2000, blank=True, null=True)
    artist = models.CharField(max_length=1000)
    title = models.CharField(max_length=1000)
    duration = models.FloatField()
    requester_ip = models.CharField(max_length=45, blank=True, default="")
    requester_session_key = models.CharField(max_length=50, blank=True, default="")
    requester_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    priority_tier = models.CharField(max_length=16, default="normal", db_index=True)
    review_status = models.CharField(max_length=16, default="clear", db_index=True)
    review_reason = models.CharField(max_length=500, blank=True, default="")
    lyrics = models.TextField(blank=True, default="")
    profanity_count = models.PositiveIntegerField(default=0)
    slur_count = models.PositiveIntegerField(default=0)
    artwork_url = models.CharField(max_length=2000, blank=True, default="")
    genre = models.CharField(max_length=250, blank=True, default="")
    objects = song_queue.SongQueue()

    def __str__(self) -> str:
        return f"{self.index}: {self.title} ({self.internal_url})"

    def displayname(self) -> str:
        """Formats the song using the utility method."""
        return song_utils.displayname(self.artist, self.title)

    class Meta:
        ordering = ["index"]


class CurrentSong(models.Model):
    """Stores the currently playing song. Only has one element."""

    queue_key = models.IntegerField()
    occurrence_id = models.UUIDField(default=uuid.uuid4, db_index=True, editable=False)
    playback_started_at = models.DateTimeField(null=True, blank=True)
    playback_outcome = models.CharField(max_length=20, default="completed")
    manually_requested = models.BooleanField()
    votes = models.IntegerField()
    artist = models.CharField(max_length=1000)
    title = models.CharField(max_length=1000)
    duration = models.FloatField()
    internal_url = models.CharField(max_length=2000)
    external_url = models.CharField(max_length=2000)
    stream_url = models.CharField(max_length=2000, blank=True, null=True)
    requester_ip = models.CharField(max_length=45, blank=True, default="")
    requester_session_key = models.CharField(max_length=50, blank=True, default="")
    requester_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    artwork_url = models.CharField(max_length=2000, blank=True, default="")
    genre = models.CharField(max_length=250, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    last_paused = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.title + " (" + self.internal_url + ")"

    def displayname(self) -> str:
        """Formats the song using the utility method."""
        return song_utils.displayname(self.artist, self.title)


class RequestLog(models.Model):
    """Stores the request of a client and its result."""

    created = models.DateTimeField(auto_now_add=True)
    song = models.ForeignKey(
        "ArchivedSong", on_delete=models.SET_NULL, blank=True, null=True
    )
    playlist = models.ForeignKey(
        "ArchivedPlaylist", on_delete=models.SET_NULL, blank=True, null=True
    )
    session_key = models.CharField(max_length=50)

    def item_displayname(self) -> str:
        """Returns the displayname of the song or the title of the playlist"""
        if self.song is not None:
            return self.song.displayname()
        if self.playlist is not None:
            return self.playlist.title
        return "Unknown"

    def __str__(self) -> str:
        if self.song is not None:
            return self.session_key + ": " + self.song.displayname()
        if self.playlist is not None:
            return self.session_key + ": " + self.playlist.title
        return self.session_key + ": <None>"


class PlayLog(models.Model):
    """Stores the log of a played song."""

    created = models.DateTimeField(auto_now_add=True)
    song = models.ForeignKey(
        "ArchivedSong", on_delete=models.SET_NULL, blank=True, null=True
    )
    manually_requested = models.BooleanField()
    votes = models.IntegerField(null=True)

    def song_displayname(self) -> str:
        """Returns the displayname of the song (if present)"""
        if not self.song:
            return "Unknown"
        return self.song.displayname()

    def __str__(self) -> str:
        return (
            "played " + self.song_displayname() + " with " + str(self.votes) + " votes"
        )


class RecentPlay(models.Model):
    """Minimal rolling history used by mandatory FURATIC replay limits."""

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    song_url = models.CharField(max_length=2000, db_index=True)


class QueueMutationLock(models.Model):
    """One short-lived transaction lock shared by queue handoffs and votes."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)


class SongVoteState(models.Model):
    """Durable presentation state, independent of transient queued row IDs."""

    occurrence_id = models.UUIDField(primary_key=True)
    engagement = models.JSONField(default=dict)
    retired_at = models.DateTimeField(null=True, blank=True, db_index=True)


class SongVote(models.Model):
    occurrence = models.ForeignKey(SongVoteState, on_delete=models.CASCADE, related_name="voters")
    voter_key = models.CharField(max_length=80)
    choice = models.SmallIntegerField(default=0)
    revision = models.PositiveIntegerField(default=0)
    changed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    activity_recorded = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["occurrence", "voter_key"], name="unique_song_voter")]
        indexes = [models.Index(fields=["voter_key", "changed_at"], name="song_voter_activity")]


class VoteMutation(models.Model):
    occurrence = models.ForeignKey(SongVoteState, on_delete=models.CASCADE, related_name="mutations")
    voter_key = models.CharField(max_length=80)
    mutation_id = models.CharField(max_length=64)
    created = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["occurrence", "voter_key", "mutation_id"], name="unique_vote_mutation")]


class PlaybackHistory(models.Model):
    """Public metadata for occurrences that really entered playback."""

    occurrence_id = models.UUIDField(unique=True)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(db_index=True)
    artist = models.CharField(max_length=1000)
    title = models.CharField(max_length=1000)
    external_url = models.CharField(max_length=2000)
    artwork_url = models.CharField(max_length=2000, blank=True, default="")
    duration = models.FloatField()
    votes = models.IntegerField(default=0)
    outcome = models.CharField(max_length=20, default="completed")

    class Meta:
        ordering = ["-ended_at", "-id"]


class Setting(models.Model):
    """key value storage for persistent settings."""

    key = models.CharField(max_length=200, unique=True)
    value = models.CharField(max_length=1000)

    def __str__(self) -> str:
        return self.key + ": " + ("None" if self.value is None else self.value)


class ModeratorProfile(models.Model):
    """FURATIC-specific moderator label and queue-only permission."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    label = models.CharField(max_length=120)
    song_only = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.label


class ClientIdentity(models.Model):
    """Stable, random browser identity used for codenames across IP changes."""

    token_hash = models.CharField(max_length=64, unique=True)
    codename = models.CharField(max_length=40, unique=True, db_index=True)
    first_ip = models.CharField(max_length=45, blank=True, default="")
    last_ip = models.CharField(max_length=45, blank=True, default="")
    created = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.codename


class AuditEntry(models.Model):
    """Searchable event-sized moderator audit history."""

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=120, db_index=True)
    actor = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=20, blank=True, default="")
    ip = models.CharField(max_length=45, blank=True, default="", db_index=True)
    codename = models.CharField(max_length=40, blank=True, default="", db_index=True)
    browser_token = models.CharField(max_length=16, blank=True, default="")
    target = models.CharField(max_length=500, blank=True, default="")
    song_key = models.IntegerField(blank=True, null=True)
    song_title = models.CharField(max_length=2000, blank=True, default="")
    metadata = models.JSONField(default=dict)

    class Meta:
        ordering = ["-created"]
