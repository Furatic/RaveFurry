"""This module handles all requests concerning the addition of music to the queue."""

from __future__ import annotations

import ast
import importlib
import logging
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, cast

from django.conf import settings as conf
from django.core.handlers.wsgi import WSGIRequest
from django.forms.models import model_to_dict
from django.db import transaction
from django.db.models import Case, F, IntegerField, Value, When
from django.http import HttpResponseBadRequest
from django.http.response import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.csrf import csrf_exempt

from core import audit_log, base, obs_export, redis, site_mode, user_manager, util
from core.models import CurrentSong, QueuedSong
from core.musiq import audio_tail, controller, next_up, playback, song_utils
from core.musiq.music_provider import MusicProvider, ProviderError, WrongUrlError
from core.musiq.playlist_provider import PlaylistProvider
from core.musiq.song_provider import SongProvider
from core.settings import storage
from core.state_handler import send_state
from core.settings.storage import PlatformEnabled, PlatformSuggestions

queue = QueuedSong.objects

def ordered_queue_queryset():
    """Return the queue in the same effective order used for playback.

    Votes >= 1 keep their relative priority.
    Votes < 1 are all treated equally for ordering.
    The locked next-up song always remains first while the current song is active.
    """
    all_songs = queue.all().annotate(queue_rank=Case(When(review_status="pending", then=Value(2)), When(priority_tier="extra", then=Value(1)), default=Value(0), output_field=IntegerField()))
    voting_enabled = storage.get("interactivity") in [
        storage.Interactivity.upvotes_only,
        storage.Interactivity.full_voting,
    ]

    if voting_enabled:
        natural_order = all_songs.annotate(
            effective_votes=Case(
                When(priority_tier="normal", votes__gte=1, then=F("votes")),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("queue_rank", "-effective_votes", "index")
    else:
        natural_order = all_songs.order_by("queue_rank", "index")

    locked_key = next_up.resolve_locked_queue_key(
        natural_order.filter(
            priority_tier="normal",
            review_status__in=["clear", "approved"],
        ).exclude(internal_url=None).values_list("id", flat=True),
        allow_new_lock=CurrentSong.objects.exists(),
    )

    if voting_enabled:
        ordered = all_songs.annotate(
            effective_votes=Case(
                When(priority_tier="normal", votes__gte=1, then=F("votes")),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        if locked_key is not None:
            ordered = ordered.annotate(
                next_up_locked=Case(
                    When(id=locked_key, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            ).order_by("next_up_locked", "queue_rank", "-effective_votes", "index")
        else:
            ordered = ordered.order_by("queue_rank", "-effective_votes", "index")
        return ordered

    if locked_key is not None:
        return all_songs.annotate(
            next_up_locked=Case(
                When(id=locked_key, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("next_up_locked", "queue_rank", "index")

    return all_songs.order_by("queue_rank", "index")

if TYPE_CHECKING:
    from core.musiq.song_utils import Metadata


def start() -> None:
    """Initializes the required modules."""

    controller.start()
    playback.start()


def get_alarm_metadata() -> "Metadata":
    """Returns a metadata object for the alarm. The duration is dynamically determined."""
    return {
        "artist": "Raveberry",
        "title": "ALARM!",
        "duration": song_utils.get_metadata(
            os.path.join(conf.BASE_DIR, "resources/sounds/alarm.m4a")
        )["duration"],
        "internal_url": "alarm",
        "external_url": "https://raveberry.party/alarm",
        "stream_url": None,
        "cached": True,
    }


def enabled_platforms_by_priority() -> List[str]:
    """Returns a list of all available platforms, ordered by priority."""
    # local music can only be searched explicitly by key and thus is last
    return [
        platform
        for platform in ["spotify", "youtube", "soundcloud", "jamendo", "local"]
        if storage.get(cast(PlatformEnabled, f"{platform}_enabled"))
    ]


def get_providers(
    query: str,
    key: Optional[int] = None,
    playlist: bool = False,
    preferred_platform: Optional[str] = None,
) -> List[MusicProvider]:
    """Returns a list of all available providers for the given query, ordered by priority.
    If a preferred platform is given, that provider will be first."""

    if key is not None:
        # an archived entry was requested.
        # The key determines the Provider
        provider: MusicProvider
        try:
            if playlist:
                provider = PlaylistProvider.create(query, key)
            else:
                provider = SongProvider.create(query, key)
            return [provider]
        except ProviderError:
            # No provider is available for the requested key
            # This might be because the platform of that archived song is not available anymore
            # -> treat the request like a search query
            # delete the key so the archived song for the different platform is disregarded
            key = None

    providers: List[MusicProvider] = []
    for platform in enabled_platforms_by_priority():
        module = importlib.import_module(f"core.musiq.{platform}")
        if playlist:
            provider_class = getattr(module, f"{platform.title()}PlaylistProvider")
        else:
            provider_class = getattr(module, f"{platform.title()}SongProvider")
        try:
            provider = provider_class(query, key)
            if platform == preferred_platform:
                providers.insert(0, provider)
            else:
                providers.append(provider)
        except WrongUrlError:
            pass

    if not providers:
        raise ProviderError("No backend configured to handle your request.")

    return providers


def try_providers(
    session_key: str,
    providers: List[MusicProvider],
    requester_ip: str = "",
    defer_enqueue: bool = False,
) -> MusicProvider:
    """Goes through every given provider and tries to request its music.
    Returns the first provider that was successful with an empty error.
    If unsuccessful, return the last provider.
    """
    fallback = False
    last_provider = providers[-1]
    provider = providers[0]

    for provider in providers:
        try:
            provider.request(
                session_key,
                requester_ip=requester_ip,
                defer_enqueue=defer_enqueue,
            )
            # the current provider could provide the song, don't try the other ones
            break
        except ProviderError as error:
            # this provider cannot provide this song, use the next provider
            if not provider.error:
                provider.error = str(error)
            # if this was the last provider, show its error
            # in new music only mode, do not allow fallbacks
            if storage.get("new_music_only") or provider == last_provider:
                return provider

            fallback = True
            provider.error = ""
        except Exception:  # pylint: disable=broad-except
            logging.exception(
                "provider request failed for type=%s query=%r key=%r",
                provider.type,
                provider.query,
                provider.key,
            )

            if isinstance(provider, SongProvider) and provider.queued_song:
                try:
                    provider.remove_placeholder()
                except Exception:  # pylint: disable=broad-except
                    logging.exception(
                        "failed to remove placeholder after provider request failure"
                    )

            provider.error = "Sorry, this request timed out or failed. Please try again."

            if storage.get("new_music_only") or provider == last_provider:
                return provider

            fallback = True
            provider.error = ""

    if fallback:
        provider.ok_message += " (used fallback)"

    return provider

def _format_cooldown_remaining(seconds: int) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"

    minutes = seconds // 60
    remaining_seconds = seconds % 60

    if remaining_seconds == 0:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    return (
        f"{minutes} minute{'s' if minutes != 1 else ''} "
        f"and {remaining_seconds} second{'s' if remaining_seconds != 1 else ''}"
    )

# accessed by the discord bot
@csrf_exempt
@user_manager.tracked
def request_music(request: WSGIRequest) -> HttpResponse:
    """Endpoint to request music."""
    if site_mode.is_afterhours():
        return HttpResponseBadRequest("FURATIC is currently in After Hours mode.")
    if site_mode.is_closing():
        return HttpResponseBadRequest(
            "Closing time: we are no longer accepting new songs. "
            "Thanks for coming!"
        )

    key_param = request.POST.get("key")
    query = request.POST.get("query")
    playlist = request.POST.get("playlist") == "true"
    platform = request.POST.get("platform")

    if query is None:
        return HttpResponseBadRequest("No query given")

    requester_ip = user_manager.get_client_ip(request)
    session_key = user_manager.normalize_session_key(request.session.session_key or "")
    identity = user_manager.client_identity(request)
    requester_token = identity.token_hash

    cooldown_remaining = user_manager.request_cooldown_remaining(
        requester_ip,
        session_key,
    )
    if cooldown_remaining > 0:
        return HttpResponseBadRequest(
            "Please wait "
            f"{_format_cooldown_remaining(cooldown_remaining)} "
            "before adding another song."
        )

    if playlist and storage.get("ip_checking"):
        return HttpResponseBadRequest(
            "Playlist requests are disabled while IP-based anti-spam is enabled."
        )

    key = None
    if key_param:
        key = int(key_param)

    try:
        providers = get_providers(query, key, playlist, platform)
    except ProviderError as error:
        return HttpResponseBadRequest(str(error))

    if playlist:
        provider = try_providers(
            session_key,
            providers,
            requester_ip=requester_ip,
        )
        if provider.error:
            return HttpResponseBadRequest(provider.error)
        user_manager.remember_request_cooldown(requester_ip, session_key)
        return JsonResponse({"message": provider.ok_message, "key": None})

    provider = try_providers(
        session_key,
        providers,
        requester_ip=requester_ip,
        defer_enqueue=True,
    )
    if provider.error:
        return HttpResponseBadRequest(provider.error)

    assert isinstance(provider, SongProvider)
    queued_song = provider.queued_song
    if not queued_song:
        logging.error(
            "no placeholder was created for query '%s' and key '%s'", query, key
        )
        return HttpResponseBadRequest("No placeholder was created")

    with transaction.atomic():
        # Serialize ownership assignment after placeholder creation; worker
        # threads must never be asked to use an uncommitted queue row.
        from core import queue_lock
        queue_lock.acquire()
        if not site_mode.accepts_song_requests():
            try:
                queue.remove(queued_song.id)
            except QueuedSong.DoesNotExist:
                pass
            transaction.on_commit(playback.queue_changed.set)
            return HttpResponseBadRequest(
                "Closing time: we are no longer accepting new songs. Thanks for coming!"
                if site_mode.is_closing() else "FURATIC is currently in After Hours mode."
            )
        from core.models import ClientIdentity  # pylint: disable=import-outside-toplevel

        ClientIdentity.objects.select_for_update().get(pk=identity.pk)
        active_count = (
            queue.filter(requester_token=requester_token).count()
            + CurrentSong.objects.filter(requester_token=requester_token).count()
        )
        if active_count >= 5:
            try:
                playback.queue.remove(queued_song.id)
            except QueuedSong.DoesNotExist:
                pass
            return HttpResponseBadRequest("You may have up to 5 active songs at once.")
        assert isinstance(provider, SongProvider)
        queue_key = queued_song.id
        has_primary = (
            queue.filter(
                requester_token=requester_token,
                priority_tier="normal",
            ).exclude(id=queue_key).exists()
            or CurrentSong.objects.filter(requester_token=requester_token).exists()
        )
        queue.filter(id=queue_key).update(
            requester_token=requester_token,
            priority_tier="extra" if has_primary else "normal",
        )
        queued_song.refresh_from_db()
        from core import voting
        voting.seed_requester_vote(request, queued_song)
        queue.rebalance_priorities()

    user_manager.remember_requester_ip(
        requester_ip,
        queue_key,
        session_key=session_key,
    )

    if storage.get("ip_checking"):
        user_manager.try_vote(requester_ip, queue_key, 1, record_activity=False)

    if storage.get("color_indication") != storage.Privileges.nobody:
        user_manager.register_song(request, queue_key)
        user_manager.register_vote(request, queue_key, 1)

    audit_log.append(
        "user_add_song",
        request=request,
        target="queue",
        song_key=queue_key,
        song_title=queued_song.displayname(),
    )
    update_state()
    provider.submission_token = requester_token
    song_number = active_count + 1
    ordinal = str(song_number) + {2: "nd", 3: "rd"}.get(song_number, "th")
    provider.submission_message = (
        f"Added your {ordinal} song to the pre-queue."
        if has_primary else provider.ok_message
    )
    provider.submission_query = query
    user_manager.remember_request_cooldown(requester_ip, session_key)
    provider.start_deferred_enqueue()

    return JsonResponse({"message": provider.submission_message, "key": queue_key, "pending": True})


@user_manager.tracked
def request_radio(request: WSGIRequest) -> HttpResponse:
    """Endpoint to request radio for the current song."""
    if site_mode.is_afterhours():
        return HttpResponseBadRequest("FURATIC is currently in After Hours mode.")
    if site_mode.is_closing():
        return HttpResponseBadRequest(
            "Closing time: we are no longer accepting new songs. "
            "Thanks for coming!"
        )
    try:
        current_song = CurrentSong.objects.get()
    except CurrentSong.DoesNotExist:
        return HttpResponseBadRequest("Need a song to play the radio")
    provider = SongProvider.create(external_url=current_song.external_url)
    return provider.request_radio(request.session.session_key)


@user_manager.tracked
def index(request: WSGIRequest) -> HttpResponse:
    """Renders the /musiq page."""
    from core import urls

    context = base.context(request)
    context["urls"] = urls.musiq_paths
    context["additional_keywords"] = storage.get("additional_keywords")
    context["forbidden_keywords"] = storage.get("forbidden_keywords")
    context["client_streaming"] = storage.get("output") == "client"
    context["show_stream"] = storage.get("output") in ["client", "icecast"] and (
        not storage.get("privileged_stream") or context["controls_enabled"]
    )
    for platform in ["youtube", "spotify", "soundcloud", "jamendo"]:
        if (
            storage.get("online_suggestions")
            and not storage.get("new_music_only")
            and storage.get(cast(PlatformEnabled, f"{platform}_enabled"))
        ):
            suggestion_count = storage.get(
                cast(PlatformSuggestions, f"{platform}_suggestions")
            )
        else:
            suggestion_count = 0
        context[f"{platform}_suggestions"] = suggestion_count
    return render(request, "musiq.html", context)

@user_manager.tracked
@xframe_options_sameorigin
def embed(request: WSGIRequest) -> HttpResponse:
    """Renders the embedded player page for /p/."""
    if site_mode.is_afterhours():
        return redirect("afterhours")
    from core import urls

    context = base.context(request)
    context["urls"] = urls.musiq_paths
    context["interactivity"] = storage.get("interactivity")
    context["controls_enabled"] = False
    context["additional_keywords"] = storage.get("additional_keywords")
    context["forbidden_keywords"] = storage.get("forbidden_keywords")
    context["client_streaming"] = storage.get("output") == "client"
    context["show_stream"] = storage.get("output") in ["client", "icecast"] and (
        not storage.get("privileged_stream") or context["controls_enabled"]
    )
    for platform in ["youtube", "spotify", "soundcloud", "jamendo"]:
        if (
            storage.get("online_suggestions")
            and not storage.get("new_music_only")
            and storage.get(cast(PlatformEnabled, f"{platform}_enabled"))
        ):
            suggestion_count = storage.get(
                cast(PlatformSuggestions, f"{platform}_suggestions")
            )
        else:
            suggestion_count = 0
        context[f"{platform}_suggestions"] = suggestion_count

    return base._disable_dynamic_page_cache(
        render(request, "musiq_embed.html", context)
    )

def _add_color_indication(engagement, song_dict) -> None:
    requested_by = None
    votes = {}
    if engagement is not None:
        requested_by, votes = ast.literal_eval(engagement)
    song_dict["requestedBy"] = user_manager.color_of(requested_by)
    song_dict["requesterVote"] = votes.get(requested_by, 0)
    song_dict["upvotes"] = []
    song_dict["downvotes"] = []
    for session_key, amount in votes.items():
        if session_key == requested_by:
            continue
        color = user_manager.color_of(session_key)
        if amount > 0:
            song_dict["upvotes"].append(color)
        else:
            song_dict["downvotes"].append(color)


def state_dict() -> Dict[str, Any]:
    """Extends the base state with musiq-specific information and returns it."""
    state = base.state_dict()

    musiq_state: Dict[str, Any] = {}

    musiq_state["siteMode"] = site_mode.get_mode()
    musiq_state["paused"] = storage.get("paused")
    # Keep the state key for older clients, but ignore previously saved shuffle.
    musiq_state["shuffle"] = False
    musiq_state["repeat"] = storage.get("repeat")
    musiq_state["autoplay"] = storage.get("autoplay")
    musiq_state["volume"] = storage.get("volume")

    try:
        current_song = CurrentSong.objects.get()
        current_song_dict = model_to_dict(current_song)
        current_song_dict["occurrence_id"] = str(current_song.occurrence_id)
        current_song_dict = util.camelize(current_song_dict)

        original_duration = float(current_song.duration or 0.0)
        effective_duration = audio_tail.effective_duration_for_song(current_song)
        if effective_duration <= 0:
            effective_duration = original_duration

        current_song_dict["originalDuration"] = original_duration
        current_song_dict["effectiveDuration"] = effective_duration
        current_song_dict["trimmedTailSeconds"] = max(
            0.0,
            original_duration - effective_duration,
        )

        # For the currently-playing song, the UI should treat duration as the
        # audible duration, not the raw media file duration with quiet tail.
        current_song_dict["duration"] = effective_duration
        current_song_dict["durationFormatted"] = song_utils.format_seconds(
            effective_duration
        )
        current_song_dict["effectiveDurationFormatted"] = song_utils.format_seconds(
            effective_duration
        )

        if storage.get("color_indication") != storage.Privileges.nobody:
            engagement = redis.connection.get(f"engagement-{current_song.queue_key}")
            _add_color_indication(engagement, current_song_dict)

        musiq_state["currentSong"] = current_song_dict

        paused = storage.get("paused")
        if paused:
            progress_seconds = (
                current_song.last_paused - current_song.created
            ).total_seconds()
        else:
            progress_seconds = (timezone.now() - current_song.created).total_seconds()

        progress_seconds = max(0.0, min(effective_duration, progress_seconds))
        musiq_state["progressSeconds"] = progress_seconds
        musiq_state["effectiveDuration"] = effective_duration

        try:
            progress = progress_seconds / effective_duration
        except ZeroDivisionError:
            progress = 1
        musiq_state["progress"] = progress * 100
    except CurrentSong.DoesNotExist:
        musiq_state["currentSong"] = None
        musiq_state["paused"] = True
        musiq_state["progress"] = 0

    song_queue = []
    total_time = 0
    locked_queue_key = next_up.get_locked_queue_key()
    for song in ordered_queue_queryset():
        if not song.internal_url:
            # Reserve ownership/limits privately until the downloaded song is ready.
            continue
        song_dict = model_to_dict(song)
        song_dict["occurrence_id"] = str(song.occurrence_id)
        song_dict = util.camelize(song_dict)
        song_dict["isNextUpLocked"] = locked_queue_key is not None and song.id == locked_queue_key
        song_dict["durationFormatted"] = song_utils.format_seconds(
            song_dict["duration"]
        )
        if storage.get("color_indication"):
            engagement = redis.connection.get(f"engagement-{song.id}")
            _add_color_indication(engagement, song_dict)
        song_queue.append(song_dict)
        if song_dict["duration"] < 0:
            # skip duration of placeholders
            continue
        total_time += song_dict["duration"]
    musiq_state["totalTimeFormatted"] = song_utils.format_seconds(total_time)
    musiq_state["songQueue"] = song_queue

    if state["alarm"]:
        musiq_state["currentSong"] = {
            "queueKey": -1,
            "manuallyRequested": False,
            "votes": 0,
            "created": "",
            # https://github.com/python/mypy/issues/4976
            **util.camelize(cast(Dict[Any, Any], get_alarm_metadata())),
        }
        musiq_state["progress"] = 0
        musiq_state["paused"] = False
    elif redis.get("backup_playing"):
        musiq_state["currentSong"] = {
            "queueKey": -1,
            "manuallyRequested": False,
            "votes": 0,
            "internalUrl": "backup_stream",
            "externalUrl": storage.get("backup_stream"),
            "artist": "",
            "title": "Backup Stream",
            "duration": 60 * 60 * 24,
            "created": "",
        }
        musiq_state["paused"] = False

    state["musiq"] = musiq_state
    return state

def update_state() -> None:
    """Sends an update event to all connected clients and refreshes OBS exports."""
    state = state_dict()
    obs_export.write_from_state(state)
    send_state(state)
