"""Twelve-hour public playback history; independent of replay-limit records."""
import datetime
import logging
from functools import wraps
from django.db import DatabaseError, transaction

from django.utils import timezone

from core import models


logger = logging.getLogger(__name__)


def _optional_history(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        try:
            # A savepoint prevents an optional history failure from poisoning
            # the queue handoff's outer transaction or stopping live music.
            with transaction.atomic():
                return func(*args, **kwargs)
        except DatabaseError:
            logger.exception("could not update optional playback history")
    return wrapped


@_optional_history
def mark_started(song) -> None:
    if song.playback_started_at is None:
        started = timezone.now()
        models.CurrentSong.objects.filter(pk=song.pk, playback_started_at__isnull=True).update(playback_started_at=started)
        song.playback_started_at = models.CurrentSong.objects.filter(pk=song.pk).values_list("playback_started_at", flat=True).first()


@_optional_history
def record_finished(song, outcome=None) -> None:
    if song.playback_started_at is not None:
        models.PlaybackHistory.objects.get_or_create(
            occurrence_id=song.occurrence_id,
            defaults={
                "started_at": song.playback_started_at,
                "ended_at": timezone.now(),
                "artist": song.artist,
                "title": song.title,
                "external_url": song.external_url,
                "artwork_url": song.artwork_url,
                "duration": song.duration,
                "votes": song.votes,
                "outcome": outcome or song.playback_outcome,
            },
        )
    models.PlaybackHistory.objects.filter(ended_at__lt=timezone.now() - datetime.timedelta(hours=12)).delete()
