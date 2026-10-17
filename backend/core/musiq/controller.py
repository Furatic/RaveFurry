"""This module handles all controls that change the playback."""

from __future__ import annotations

import datetime
import subprocess
import re
import uuid
from functools import wraps
from typing import Callable

from django.conf import settings as conf
from django.core.handlers.wsgi import WSGIRequest
from django.db import transaction
from django.db.models import F
from django.http import HttpResponseForbidden, JsonResponse
from django.http.response import HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from core import audit_log, models, redis, ui_notifications, user_manager, playback_state_backup
from core.musiq import musiq, playback, player
from core.settings import storage
from core.util import extract_value

SEEK_DISTANCE = 10


def control(func: Callable) -> Callable:
    """A decorator for functions that control the playback.
    Every control changes the views state and returns an empty response.
    At least mod privilege is required during voting."""

    def _decorator(request: WSGIRequest) -> HttpResponse:
        if (
            storage.get("interactivity") != storage.Interactivity.full_control
            and not user_manager.has_controls(request.user)
            and not user_manager.has_secret_controls(request)
        ):
            return HttpResponseForbidden()
        response = func(request)
        musiq.update_state()
        if response is not None:
            return response
        return HttpResponse()

    return wraps(func)(_decorator)


def start() -> None:
    """Initializes this module by restoring the volume."""
    volume = storage.get("volume")
    _set_volume(volume)


@control
def restart(_request: WSGIRequest) -> None:
    """Restarts the current song from the beginning."""
    player.restart()
    try:
        current_song = models.CurrentSong.objects.get()
        current_song.created = timezone.now()
        current_song.save(update_fields=['created'])
        playback_state_backup.snapshot()
    except models.CurrentSong.DoesNotExist:
        pass


@control
def seek_backward(_request: WSGIRequest) -> None:
    """Jumps back in the current song."""
    player.seek_backward(SEEK_DISTANCE)
    try:
        current_song = models.CurrentSong.objects.get()
        now = timezone.now()
        current_song.created += datetime.timedelta(seconds=SEEK_DISTANCE)
        current_song.created = min(current_song.created, now)
        current_song.save(update_fields=['created'])
        playback_state_backup.snapshot()
    except models.CurrentSong.DoesNotExist:
        pass


def _resume() -> None:
    player.play()
    try:
        # move the creation timestamp into the future for the duration of the pause
        # this ensures that the progress calculation (starting from created) is correct
        current_song = models.CurrentSong.objects.get()
        now = timezone.now()
        pause_duration = (now - current_song.last_paused).total_seconds()
        current_song.created += datetime.timedelta(seconds=pause_duration)
        current_song.created = min(current_song.created, now)
        current_song.save(update_fields=['created'])
    except models.CurrentSong.DoesNotExist:
        pass

    storage.put("paused", False)
    redis.put("paused", False)
    playback_state_backup.snapshot()

@control
def play(_request: WSGIRequest) -> None:
    """Resumes the current song if it is paused.
    No-op if already playing."""
    _resume()


def _pause() -> None:
    player.pause()
    try:
        current_song = models.CurrentSong.objects.get()
        current_song.last_paused = timezone.now()
        current_song.save(update_fields=['last_paused'])
    except models.CurrentSong.DoesNotExist:
        pass

    storage.put("paused", True)
    redis.put("paused", True)
    playback_state_backup.snapshot()


@control
def pause(_request: WSGIRequest) -> None:
    """Pauses the current song if it is playing.
    No-op if already paused."""
    _pause()


@control
def seek_forward(_request: WSGIRequest) -> None:
    """Jumps forward in the current song."""
    player.seek_forward(SEEK_DISTANCE)
    try:
        current_song = models.CurrentSong.objects.get()
        current_song.created -= datetime.timedelta(seconds=SEEK_DISTANCE)
        current_song.save(update_fields=['created'])
        playback_state_backup.snapshot()
    except models.CurrentSong.DoesNotExist:
        pass


def _skip(reason: str = "manual", expected_occurrence=None) -> None:
    skipped_song_title = ""

    try:
        current_song = models.CurrentSong.objects.get()
        if expected_occurrence is not None and current_song.occurrence_id != expected_occurrence:
            return
        skipped_song_title = current_song.displayname()
    except models.CurrentSong.DoesNotExist:
        current_song = None

    player.skip()
    redis.put("backup_playing", False)

    if current_song is not None:
        current_song.created = timezone.now() - datetime.timedelta(
            seconds=current_song.duration
        )
        current_song.playback_outcome = "skipped"
        current_song.save(update_fields=["created", "playback_outcome"])
        playback_state_backup.snapshot()

    if not skipped_song_title:
        return

    if reason == "downvote":
        ui_notifications.emit(
            "downvote_skip",
            "Downvoted song skipped",
            f"{skipped_song_title} hit -2 votes and was skipped.",
            level="warning",
            icon="👎",
        )
    elif reason == "moderator":
        ui_notifications.emit(
            "moderator_skip",
            "Skipped by moderator",
            f"{skipped_song_title} was skipped by a moderator.",
            level="info",
            icon="⏭",
        )
    elif reason == "manual":
        ui_notifications.emit(
            "song_skip",
            "Song skipped",
            f"{skipped_song_title} was skipped.",
            level="info",
            icon="⏭",
        )


@control
def skip(_request: WSGIRequest) -> None:
    """Skips the current song and continues with the next one."""
    _skip(reason="manual")


@control
def set_shuffle(_request: WSGIRequest) -> None:
    """Keep the legacy endpoint safe for already-open clients: shuffle is retired."""
    storage.put("shuffle", False)


@control
def set_repeat(request: WSGIRequest) -> None:
    """Enables or disables repeat depending on the given value.
    If enabled, a song is enqueued again after it finished playing."""
    enabled = request.POST.get("value") == "true"
    storage.put("repeat", enabled)


@control
def set_autoplay(request: WSGIRequest) -> None:
    """Enables or disables autoplay depending on the given value.
    If enabled and the current song is the last one,
    a new song is enqueued, based on the current one."""
    enabled = request.POST.get("value") == "true"
    storage.put("autoplay", enabled)
    playback.handle_autoplay()


def _set_volume(volume) -> None:
    try:
        # Try to set the volume via the pulse server.
        # This is faster and does not impact visualization
        subprocess.run(
            f"pactl set-sink-volume @DEFAULT_SINK@ {round(volume*100)}%".split(),
            env={"PULSE_SERVER": conf.PULSE_SERVER},
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        # pulse is not installed or there is no server running.
        # TODO: why does this hang with spotipy?
        # it can't change the volume on the phone, but it should simply raise an error which gets catched and then move on
        player.set_volume(volume)


@control
def set_volume(request: WSGIRequest) -> HttpResponse:
    """Sets the playback volume.
    value has to be a float between 0 and 1."""
    volume, response = extract_value(request.POST)
    _set_volume(float(volume))
    storage.put("volume", float(volume))
    return response


@control
def shuffle_all(request: WSGIRequest) -> HttpResponse:
    """Accept legacy admin requests without changing the fair queue order."""
    if not user_manager.is_admin(request.user):
        return HttpResponseForbidden()
    storage.put("shuffle", False)
    return HttpResponse()


@control
def remove_all(request: WSGIRequest) -> HttpResponse:
    """Empties the queue. Only admin is permitted to do this."""
    if not user_manager.is_admin(request.user):
        return HttpResponseForbidden()

    from core import queue_lock, voting
    with transaction.atomic():
        queue_lock.acquire()
        for occurrence in playback.queue.values_list("occurrence_id", flat=True):
            voting.retire(occurrence)
        user_manager.clear_queue_slots()
        playback.queue.all().delete()

    playback_state_backup.snapshot()
    return HttpResponse()


@control
def prioritize(request: WSGIRequest) -> HttpResponse:
    """Prioritizes song by making it the first one in the queue."""
    key_param = request.POST.get("key")
    if key_param is None:
        return HttpResponseBadRequest()
    key = int(key_param)
    playback.queue.prioritize(key)
    return HttpResponse()


@control
def remove(request: WSGIRequest) -> HttpResponse:
    """Removes a song identified by the given key from the queue."""
    key_param = request.POST.get("key")
    if key_param is None:
        return HttpResponseBadRequest()
    key = int(key_param)
    try:
        removed = playback.queue.remove(key)
        # if we removed a song and it was added by autoplay,
        # we want it to be the new basis for autoplay
        if not removed.manually_requested:
            playback.handle_autoplay(removed.external_url or removed.title)
        else:
            playback.handle_autoplay()
    except models.QueuedSong.DoesNotExist:
        return HttpResponseBadRequest("song does not exist")
    return HttpResponse()

@user_manager.tracked
def remove_own_song(request: WSGIRequest) -> HttpResponse:
    """Allow a requester to remove only their own queued song, with rate limiting."""
    key_param = request.POST.get("key")
    if key_param is None:
        return HttpResponseBadRequest("missing key")

    key = int(key_param)
    request_ip = user_manager.get_client_ip(request)
    identity = user_manager.client_identity(request, create=False)
    
    owned_by_browser = bool(
        identity
        and models.QueuedSong.objects.filter(
            id=key,
            requester_token=identity.token_hash,
        ).exists()
    )
    if not owned_by_browser and not user_manager.song_belongs_to_ip(request_ip, key):
        return HttpResponseForbidden("That is not your song.")

    if not user_manager.can_self_remove_song(request_ip):
        return HttpResponseBadRequest(
            "You have removed too many songs recently. Please wait a few minutes."
        )

    try:
        removed = playback.queue.remove(key)
    except models.QueuedSong.DoesNotExist:
        return HttpResponseBadRequest("song does not exist")

    user_manager.record_self_remove(request_ip, key)

    audit_log.append(
        "user_remove_own_song",
        request=request,
        target="queue",
        song_key=key,
        song_title=removed.displayname(),
    )

    if not removed.manually_requested:
        playback.handle_autoplay(removed.external_url or removed.title)
    else:
        playback.handle_autoplay()

    musiq.update_state()
    return HttpResponse("ok")


def own_song_state(request: WSGIRequest) -> HttpResponse:
    """Return the current requester's queued songs and current song ownership.

    Redis is used as a fast path, but requester identity is also stored on the
    queue/current song rows so tab sleep, Redis misses, or reconnects do not
    make the app forget who owns a song.
    """
    request_ip = user_manager.get_client_ip(request)
    normalized_ip = user_manager.normalize_ip(request_ip)
    identity = user_manager.client_identity(request, create=False)
    requester_token = identity.token_hash if identity else ""
    songs = []
    
    active_queue_key = user_manager.get_active_queue_slot(request_ip)
    
    for position, song in enumerate(musiq.ordered_queue_queryset(), start=1):
        owns_song = False
    
        if active_queue_key is not None and song.id == active_queue_key:
            owns_song = True
    
        if not owns_song and requester_token and song.requester_token == requester_token:
            owns_song = True
    
        # Compatibility fallback for songs queued before browser identities.
        if (
            not owns_song
            and not song.requester_token
            and normalized_ip
            and song.requester_ip == normalized_ip
        ):
            owns_song = True

        if owns_song:
            songs.append(
                {
                    "queueKey": song.id,
                    "queuePosition": position,
                }
            )

    current_song_queue_key = None
    current_song = models.CurrentSong.objects.first()
    if (
        current_song
        and (
            (requester_token and current_song.requester_token == requester_token)
            or (
                not current_song.requester_token
                and normalized_ip
                and (
                    current_song.requester_ip == normalized_ip
                    or user_manager.song_belongs_to_ip(
                        request_ip,
                        current_song.queue_key,
                    )
                )
            )
        )
    ):
        current_song_queue_key = current_song.queue_key

    from core import voting
    active_songs = list(musiq.ordered_queue_queryset())
    if current_song:
        active_songs.append(current_song)
    response = JsonResponse({
        "songs": songs,
        "currentSongQueueKey": current_song_queue_key,
        "myVotes": voting.personal_state(request, active_songs),
    })
    response["Cache-Control"] = "private, no-store"
    return response

@control
def reorder(request: WSGIRequest) -> HttpResponse:
    """Reorders the queue.
    The song specified by element is inserted between prev and next."""
    prev_key_param = request.POST.get("prev")
    cur_key_param = request.POST.get("element")
    next_key_param = request.POST.get("next")
    if not cur_key_param:
        return HttpResponseBadRequest()
    if not prev_key_param:
        prev_key = None
    else:
        prev_key = int(prev_key_param)
    cur_key = int(cur_key_param)
    if not next_key_param:
        next_key = None
    else:
        next_key = int(next_key_param)
    try:
        playback.queue.reorder(prev_key, cur_key, next_key)
    except ValueError:
        return HttpResponseBadRequest("request on old state")
    return HttpResponse()


@csrf_exempt
@user_manager.tracked
def vote(request: WSGIRequest) -> HttpResponse:
    """Apply a durable vote, retaining compatibility with older delta clients."""
    from core import voting

    try:
        key = int(request.POST["key"])
        desired = int(request.POST["choice"]) if "choice" in request.POST else None
        amount = int(request.POST["amount"]) if desired is None else None
        occurrence = uuid.UUID(request.POST["occurrence"]) if request.POST.get("occurrence") else None
        revision = int(request.POST["revision"]) if request.POST.get("revision") is not None else None
        mutation = request.POST.get("mutation", "")
        if desired is not None and (desired not in (-1, 0, 1) or occurrence is None):
            raise ValueError()
        if amount is not None and (amount < -2 or amount > 2 or amount == 0):
            raise ValueError()
        if revision is not None and revision < 0:
            raise ValueError()
        if mutation and not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", mutation):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        return HttpResponseBadRequest("Invalid vote request.")
    try:
        response, removed, should_skip = voting.apply_vote(
            request, key, desired, amount, occurrence, revision, mutation,
        )
    except voting.VoteRejected as error:
        return JsonResponse({"message": str(error), **error.state}, status=error.status)
    if should_skip:
        _skip(reason="downvote", expected_occurrence=uuid.UUID(response["occurrenceId"]))
    if removed is not None:
        if not removed.manually_requested:
            playback.handle_autoplay(removed.external_url or removed.title)
        else:
            playback.handle_autoplay()
    if "amount" in response:
        audit_log.append(
            "user_vote", request=request,
            target="current-song" if models.CurrentSong.objects.filter(queue_key=key).exists() else "queue",
            song_key=key, metadata={"amount": response["amount"]},
        )
    musiq.update_state()
    return JsonResponse(response)


def played_history(request: WSGIRequest) -> HttpResponse:
    """Return one bounded page of public song metadata from the last 12 hours."""
    from django.db.models import Q

    rows = models.PlaybackHistory.objects.filter(ended_at__gte=timezone.now() - datetime.timedelta(hours=12))
    cursor = request.GET.get("before", "")
    if cursor:
        try:
            anchor = models.PlaybackHistory.objects.get(pk=int(cursor))
            rows = rows.filter(Q(ended_at__lt=anchor.ended_at) | Q(ended_at=anchor.ended_at, id__lt=anchor.pk))
        except (ValueError, models.PlaybackHistory.DoesNotExist):
            return HttpResponseBadRequest("Invalid history page.")
    page = list(rows[:21])
    more = len(page) > 20
    page = page[:20]
    response = JsonResponse({"songs": [{
        "id": row.id, "artist": row.artist, "title": row.title,
        "externalUrl": row.external_url, "artworkUrl": row.artwork_url,
        "duration": row.duration, "endedAt": row.ended_at.isoformat(),
        "outcome": row.outcome,
    } for row in page], "nextCursor": str(page[-1].id) if page and more else None})
    response["Cache-Control"] = "no-store"
    return response
