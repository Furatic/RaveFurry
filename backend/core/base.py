"""This module provides common functionality for all pages on the site."""

import datetime
import logging
import os
import random
import secrets
from typing import Any, Dict, Optional

from django.core.handlers.wsgi import WSGIRequest
from django.db import DatabaseError, transaction
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.http.response import HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone

from django.conf import settings as conf
from core import audit_log, models, redis, site_mode, user_manager
from core.lights import controller
from core.musiq import musiq
from core.settings import storage
from core.settings import system
from core.state_handler import send_state


def _get_random_hashtag() -> str:
    active_hashtags = models.Tag.objects.filter(active=True)
    if active_hashtags.count() == 0:
        return "Add your first hashtag!"
    index = random.randint(0, active_hashtags.count() - 1)
    hashtag = active_hashtags[index]
    return hashtag.text


def _get_apk_link() -> str:
    local_apk = os.path.join(conf.STATIC_FILES, "apk/shareberry.apk")
    if os.path.isfile(local_apk):
        assert conf.STATIC_URL
        return os.path.join(conf.STATIC_URL, "apk/shareberry.apk")
    return "https://github.com/raveberry/shareberry/releases/latest/download/shareberry.apk"


def _static_asset_version() -> str:
    """Return a cache-busting version for frontend assets."""
    candidates = [
        os.path.join(conf.STATIC_FILES, "bundle.js"),
        os.path.join(conf.STATIC_FILES, "style.css"),
    ]
    newest_mtime = 0
    for path in candidates:
        try:
            newest_mtime = max(newest_mtime, int(os.path.getmtime(path)))
        except OSError:
            continue
    return f"{conf.VERSION}-{newest_mtime}"


def _disable_dynamic_page_cache(response: HttpResponse) -> HttpResponse:
    """Prevent browsers from caching dynamic HTML shells."""
    response["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate, private"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def _furatic_public_context() -> Dict[str, Any]:
    """Shared public Furatic branding / link context."""
    return {
        "furatic_public_url": conf.FURATIC_PUBLIC_URL,
        "furatic_logo_url": conf.FURATIC_LOGO_SQUARE_URL,
        "furatic_logo_wide_url": conf.FURATIC_LOGO_WIDE_URL,
        "discord_invite_url": conf.FURATIC_DISCORD_INVITE_URL,
        "vrchat_group_url": conf.FURATIC_VRCHAT_GROUP_URL,
        "hls_url": conf.FURATIC_HLS_URL,
    }

RUNTIME_INSTANCE_ID = secrets.token_hex(8)

logger = logging.getLogger(__name__)
_visitor_count_cache: Optional[int] = None


def _get_visitor_count() -> int:
    """Return the visitor count without making every state poll require DB access."""
    global _visitor_count_cache

    if _visitor_count_cache is not None:
        return _visitor_count_cache

    try:
        _visitor_count_cache = models.Counter.objects.get_or_create(
            id=1,
            defaults={"value": 0},
        )[0].value
    except DatabaseError as error:
        logger.warning("failed to read visitor counter for state: %s", error)
        return 0

    return _visitor_count_cache

def _increment_counter() -> int:
    global _visitor_count_cache

    with transaction.atomic():
        counter = models.Counter.objects.get_or_create(id=1, defaults={"value": 0})[0]
        counter.value += 1
        counter.save()

    _visitor_count_cache = counter.value
    update_state()
    return counter.value


def context(request: WSGIRequest) -> Dict[str, Any]:
    """Returns the base context that is needed on every page.
    Increments the visitors counter."""
    from core import urls

    _increment_counter()
    context = {
        "base_urls": urls.base_paths,
        "interactivity": storage.get("interactivity"),
        "interactivities": {
            "fullControl": storage.Interactivity.full_control,
            "fullVoting": storage.Interactivity.full_voting,
            "upvotesOnly": storage.Interactivity.upvotes_only,
            "noControl": storage.Interactivity.no_control,
        },
        "color_indication": user_manager.has_privilege(
            request.user, storage.get("color_indication")
        ),
        "user_color": user_manager.color_of(request.session.session_key),
        "privileges": {
            "everybody": storage.Privileges.everybody,
            "mod": storage.Privileges.mod,
            "admin": storage.Privileges.admin,
            "nobody": storage.Privileges.nobody,
        },
        "hashtag": _get_random_hashtag(),
        "demo": conf.DEMO,
        "controls_enabled": user_manager.has_controls(request.user)
        or storage.get("interactivity") == storage.Interactivity.full_control,
        "is_admin": user_manager.is_admin(request.user),
        "is_moderator": user_manager.is_moderator(request.user),
        "can_moderate": user_manager.can_moderate(request.user),
        "apk_link": _get_apk_link(),
        "static_asset_version": _static_asset_version(),
        "runtime_instance_id": RUNTIME_INSTANCE_ID,
        "local_enabled": storage.get("local_enabled"),
        "youtube_enabled": storage.get("youtube_enabled"),
        "spotify_enabled": storage.get("spotify_enabled"),
        "soundcloud_enabled": storage.get("soundcloud_enabled"),
        "jamendo_enabled": storage.get("jamendo_enabled"),
    }
    context.update(_furatic_public_context())
    return context


def state_dict() -> Dict[str, Any]:
    """This function constructs a base state dictionary with website wide state.
    Pages sending states extend this state dictionary."""
    try:
        default_platform = musiq.enabled_platforms_by_priority()[0]
    except IndexError:
        default_platform = ""
    return {
        "partymode": user_manager.partymode_enabled(),
        "users": user_manager.get_count(),
        "visitors": _get_visitor_count(),
        "lightsEnabled": redis.get("lights_active"),
        "playbackError": redis.get("playback_error"),
        "alarm": redis.get("alarm_playing"),
        "defaultPlatform": default_platform,
    }


def landing(request: WSGIRequest) -> HttpResponse:
    """Renders the static page with the embedded player iframe."""
    return _disable_dynamic_page_cache(
        render(request, "landing.html", _furatic_public_context())
    )


def _afterhours_top_songs() -> list:
    """Rank unique songs by their accumulated playback scores in the last 12 hours."""
    now = timezone.now()
    cutoff = now - datetime.timedelta(hours=12)
    fields = ("occurrence_id", "external_url", "artist", "title", "artwork_url", "votes")
    history = list(models.PlaybackHistory.objects.filter(
        ended_at__gte=cutoff, ended_at__lte=now,
    ).order_by("-ended_at", "-id").values(*fields))
    # After-hours pauses the last track instead of retiring it into history.
    current = models.CurrentSong.objects.filter(
        playback_started_at__gte=cutoff, playback_started_at__lte=now,
    ).exclude(occurrence_id__in=[row["occurrence_id"] for row in history]).values(*fields)
    songs = {}
    for row in list(current) + history:
        key = row["external_url"] or (row["artist"], row["title"])
        if key not in songs:
            songs[key] = {
                "title": row["title"] or "Untitled song",
                "artist": row["artist"] or "Unknown artist",
                "artworkUrl": row["artwork_url"],
                "votes": 0,
            }
        songs[key]["votes"] += int(row["votes"] or 0)
    return sorted(songs.values(), key=lambda song: (
        -song["votes"], song["title"].casefold(), song["artist"].casefold(),
    ))[:20]


def afterhours(_request: WSGIRequest) -> HttpResponse:
    """Renders the FURATIC After Hours page."""
    page_context = _furatic_public_context()
    try:
        page_context["afterhours_top_songs"] = _afterhours_top_songs()
    except DatabaseError:
        logger.exception("could not read after-hours top songs")
        page_context["afterhours_top_songs_unavailable"] = True
    return _disable_dynamic_page_cache(
        render(_request, "landing_afterhours.html", page_context)
    )


def afterhours_top_songs(_request: WSGIRequest) -> HttpResponse:
    """Refresh the after-hours chart without reloading the landing page."""
    try:
        response = JsonResponse({"songs": _afterhours_top_songs()})
    except DatabaseError:
        logger.exception("could not refresh after-hours top songs")
        response = JsonResponse({"message": "Top songs are temporarily unavailable."}, status=503)
    return _disable_dynamic_page_cache(response)


def afterhours_history(_request: WSGIRequest) -> HttpResponse:
    """Return the fifty most recent plays, independently of the vote rankings."""
    now = timezone.now()
    try:
        rows = models.PlaybackHistory.objects.filter(
            ended_at__gte=now - datetime.timedelta(hours=12), ended_at__lte=now,
        ).order_by("-ended_at", "-id")[:50]
        response = JsonResponse({"songs": [{
            "id": row.id, "artist": row.artist, "title": row.title,
            "artworkUrl": row.artwork_url, "endedAt": row.ended_at.isoformat(),
        } for row in rows]})
    except DatabaseError:
        logger.exception("could not read after-hours playback history")
        response = JsonResponse({"message": "Recently played songs are temporarily unavailable."}, status=503)
    return _disable_dynamic_page_cache(response)


def site_mode_status(_request: WSGIRequest) -> HttpResponse:
    """Returns the current runtime-only public site mode."""
    return JsonResponse({"mode": site_mode.get_mode()})

@require_POST
def log_refresh(request: WSGIRequest) -> HttpResponse:
    """Record an actual browser reload for moderator audit visibility."""
    page = request.POST.get("page") or request.path
    audit_log.append("page_refresh", request=request, target=page)
    return HttpResponse("ok")

def settings_disabled(_request: WSGIRequest) -> HttpResponse:
    """Disable the broken settings page and send admins to Django admin instead."""
    return HttpResponseRedirect("/admin/")

def no_stream(request: WSGIRequest) -> HttpResponse:
    """Renders the /stream page. If this is reached, there is no stream active."""
    return _disable_dynamic_page_cache(
        render(request, "no_stream.html", context(request))
    )


def submit_hashtag(request: WSGIRequest) -> HttpResponse:
    """Add the given hashtag to the database."""
    hashtag = request.POST.get("hashtag")
    if hashtag is None or len(hashtag) == 0:
        return HttpResponseBadRequest()

    if hashtag[0] != "#":
        hashtag = "#" + hashtag
    models.Tag.objects.create(text=hashtag, active=storage.get("hashtags_active"))

    return HttpResponse()


def logged_in(request: WSGIRequest) -> HttpResponse:
    """This endpoint is visited after every login.
    Redirect privileged users to the appropriate dashboard."""
    if user_manager.is_admin(request.user):
        return HttpResponseRedirect("/admin/")
    if user_manager.can_moderate(request.user):
        return HttpResponseRedirect(reverse("moderator"))
    return HttpResponseRedirect(reverse("base"))


def set_user_color(request: WSGIRequest) -> None:
    """Set user color for indication of votes.
    Situated in base because the dropdown is accessible from every page."""
    return user_manager.set_user_color(request)


def set_lights_shortcut(request: WSGIRequest) -> None:
    """Request endpoint for the lights shortcut.
    Situated in base because the dropdown is accessible from every page."""
    return controller.set_lights_shortcut(request)


def upgrade_available(_request: WSGIRequest) -> HttpResponse:
    """Checks whether newer Raveberry version is available."""
    latest_version = system.fetch_latest_version()
    current_version = conf.VERSION
    if latest_version and latest_version != current_version:
        return JsonResponse(True, safe=False)
    return JsonResponse(False, safe=False)


def update_state() -> None:
    """Sends an update event to all connected clients."""
    send_state(state_dict())
