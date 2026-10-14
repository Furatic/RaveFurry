"""Custom moderator dashboard and APIs for FURATIC."""
from __future__ import annotations

from typing import Any, Dict, List

from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from core import audit_log, base, ip_screening, models, site_mode, user_manager
from core.musiq import controller as musiq_controller, musiq, playback, song_utils
from core.settings import storage


VOTING_INTERACTIVITIES = {
    storage.Interactivity.upvotes_only,
    storage.Interactivity.full_voting,
}


def _queue_queryset():
    return musiq.ordered_queue_queryset()


def _serialize_song(song, include_details: bool = False) -> Dict[str, Any]:
    queue_key = getattr(song, "queue_key", None) or getattr(song, "id")
    payload = {
        "queueKey": queue_key,
        "title": song.title,
        "artist": song.artist,
        "displayName": song.displayname(),
        "votes": getattr(song, "votes", 0),
        "duration": song.duration,
        "durationFormatted": song_utils.format_seconds(song.duration),
        "manuallyRequested": getattr(song, "manually_requested", False),
        "requesterIp": user_manager.get_song_requester_ip(queue_key),
        "externalUrl": song.external_url,
        "reviewStatus": getattr(song, "review_status", "clear"),
        "reviewReason": getattr(song, "review_reason", ""),
        "profanityCount": getattr(song, "profanity_count", 0),
        "slurCount": getattr(song, "slur_count", 0),
        "artworkUrl": getattr(song, "artwork_url", ""),
        "genre": getattr(song, "genre", ""),
        "priorityTier": getattr(song, "priority_tier", "normal"),
    }
    if include_details:
        from core.song_analysis import matched_terms

        lyrics = getattr(song, "lyrics", "")
        payload["lyrics"] = lyrics
        payload["matchedTerms"] = matched_terms(lyrics)
    return payload

def _request_settings_payload() -> Dict[str, Any]:
    return {
        "requestCooldownSeconds": float(storage.get("request_cooldown_seconds")),
        "maxSongDurationSeconds": float(storage.get("max_song_duration_seconds")),
    }

def _state_payload(request=None) -> Dict[str, Any]:
    try:
        current_song = models.CurrentSong.objects.get()
        current_payload = _serialize_song(current_song)
    except models.CurrentSong.DoesNotExist:
        current_payload = None

    queue_payload = [_serialize_song(song) for song in _queue_queryset()]
    payload = {
        "mode": site_mode.get_mode(),
        "currentSong": current_payload,
        "queue": queue_payload,
        "bannedIps": user_manager.get_banned_ips(),
        "whitelistedIps": user_manager.get_whitelisted_ips(),
        "auditLog": audit_log.get_recent(1000),
        "blocklists": ip_screening.list_blocklists(),
        "ipIntel": ip_screening.get_runtime_state(),
        "requestSettings": _request_settings_payload(),
        "isAdmin": bool(request and user_manager.is_admin(request.user)),
        "songOnly": bool(request and user_manager.is_song_only_moderator(request.user)),
        "moderatorAccounts": _moderator_accounts() if request and user_manager.is_admin(request.user) else [],
    }
    if request and user_manager.is_song_only_moderator(request.user):
        payload.update(bannedIps=[], whitelistedIps=[], auditLog=[], blocklists=[], ipIntel={}, requestSettings={})
        for song in ([payload.get("currentSong")] if payload.get("currentSong") else []) + payload["queue"]:
            song["requesterIp"] = ""
    return payload


@user_manager.moderator_required
def dashboard(request: WSGIRequest) -> HttpResponse:
    """Render the moderator dashboard."""
    context = base.context(request)
    context.update(
        {
            "is_admin": user_manager.is_admin(request.user),
            "moderator_login_url": reverse("furatic-login"),
            "moderator_state_url": reverse("moderator-state"),
            "moderator_remove_song_url": reverse("moderator-remove-song"),
            "moderator_skip_current_url": reverse("moderator-skip-current"),
            "moderator_ban_ip_url": reverse("moderator-ban-ip"),
            "moderator_unban_ip_url": reverse("moderator-unban-ip"),
            "moderator_whitelist_ip_url": reverse("moderator-whitelist-ip"),
            "moderator_unwhitelist_ip_url": reverse("moderator-unwhitelist-ip"),
            "moderator_site_mode_url": reverse("moderator-site-mode"),
            "moderator_request_settings_url": reverse("moderator-request-settings"),
            "moderator_add_blocklist_url": reverse("moderator-add-blocklist"),
            "moderator_rename_blocklist_url": reverse("moderator-rename-blocklist"),
            "moderator_remove_blocklist_url": reverse("moderator-remove-blocklist"),
            "moderator_account_save_url": reverse("moderator-account-save"),
            "moderator_account_delete_url": reverse("moderator-account-delete"),
            "moderator_identity_search_url": reverse("moderator-identity-search"),
            "moderator_audit_search_url": reverse("moderator-audit-search"),
            "moderator_review_song_url": reverse("moderator-review-song"),
            "moderator_song_details_url": reverse("moderator-song-details"),
        }
    )
    return render(request, "moderator.html", context)


@require_GET
@user_manager.moderator_required
def state(_request: WSGIRequest) -> HttpResponse:
    """Return moderator state for polling / refreshes."""
    return JsonResponse(_state_payload(_request))


def _moderator_accounts():
    users = get_user_model().objects.filter(Q(groups__name=user_manager.MODERATOR_GROUP_NAME) | Q(is_superuser=True)).distinct()
    result = []
    for user in users:
        profile = getattr(user, "moderatorprofile", None)
        result.append({"id": user.id, "username": user.get_username(), "label": profile.label if profile else user.get_username(), "songOnly": bool(profile and profile.song_only), "active": user.is_active, "admin": user.is_superuser})
    return result


@require_POST
@user_manager.admin_required
@transaction.atomic
def save_moderator_account(request):
    User = get_user_model()
    account_id = request.POST.get("id", "")
    if request.POST.get("settings_only") == "true":
        if not account_id.isdecimal():
            return HttpResponseBadRequest("Moderator account not found.")
        user = User.objects.select_for_update().filter(pk=account_id, is_superuser=False, groups__name=user_manager.MODERATOR_GROUP_NAME).first()
        if user is None:
            return HttpResponseBadRequest("Moderator account not found.")
        if any(request.POST.get(field) not in ("true", "false") for field in ("active", "song_only") if field in request.POST):
            return HttpResponseBadRequest("Invalid moderator access selection.")
        profile, _ = models.ModeratorProfile.objects.get_or_create(user=user, defaults={"label": user.get_username()})
        if "active" in request.POST:
            if request.POST["active"] not in ("true", "false"):
                return HttpResponseBadRequest("Choose enabled or disabled access.")
            user.is_active = request.POST["active"] == "true"
            user.save(update_fields=["is_active"])
        if "song_only" in request.POST:
            if request.POST["song_only"] not in ("true", "false"):
                return HttpResponseBadRequest("Choose a moderator role.")
            profile.song_only = request.POST["song_only"] == "true"
            profile.save(update_fields=["song_only"])
        transaction.on_commit(lambda: audit_log.append("admin_save_moderator", request=request, target=user.get_username(), metadata={"active": user.is_active, "songOnly": profile.song_only}))
        return JsonResponse({"accounts": _moderator_accounts()})
    username = request.POST.get("username", "").strip()
    label = request.POST.get("label", "").strip()
    password = request.POST.get("password", "")
    if not username or not label:
        return HttpResponseBadRequest("Username and label are required.")
    if len(username) > User._meta.get_field(User.USERNAME_FIELD).max_length or len(label) > models.ModeratorProfile._meta.get_field("label").max_length:
        return HttpResponseBadRequest("The username or display label is too long.")
    if password:
        try:
            validate_password(password)
        except ValidationError as error:
            return HttpResponseBadRequest(" ".join(error.messages))
    if account_id and not account_id.isdecimal():
        return HttpResponseBadRequest("Moderator account not found.")
    user = User.objects.select_for_update().filter(id=account_id, groups__name=user_manager.MODERATOR_GROUP_NAME).first() if account_id else None
    if account_id and user is None:
        return HttpResponseBadRequest("Moderator account not found.")
    if user and user.is_superuser:
        return HttpResponseBadRequest("The administrator account cannot be edited here.")
    if user is None:
        if User.objects.filter(username__iexact=username).exists():
            return HttpResponseBadRequest("That username already exists.")
        if not password:
            return HttpResponseBadRequest("A password is required for a new account.")
        user = User.objects.create_user(username=username, password=password)
    elif User.objects.filter(username__iexact=username).exclude(id=user.id).exists():
        return HttpResponseBadRequest("That username already exists.")
    user.username = username
    user.is_active = request.POST.get("active", "true").lower() == "true"
    if password and account_id:
        user.set_password(password)
    user.save()
    group, _ = Group.objects.get_or_create(name=user_manager.MODERATOR_GROUP_NAME)
    user.groups.add(group)
    profile, _ = models.ModeratorProfile.objects.get_or_create(user=user, defaults={"label": label})
    profile.label = label
    profile.song_only = request.POST.get("song_only", "false").lower() == "true"
    profile.save()
    audit_log.append("admin_save_moderator", request=request, target=username, metadata={"active": user.is_active, "songOnly": profile.song_only})
    return JsonResponse({"accounts": _moderator_accounts()})


@require_POST
@user_manager.admin_required
@transaction.atomic
def delete_moderator_account(request):
    account_id = request.POST.get("id", "")
    if not account_id.isdecimal():
        return HttpResponseBadRequest("Moderator account not found.")
    user = get_user_model().objects.select_for_update().filter(
        id=account_id,
        is_superuser=False,
        groups__name=user_manager.MODERATOR_GROUP_NAME,
    ).first()
    if not user:
        return HttpResponseBadRequest("Moderator account not found.")
    username = user.get_username()
    user.delete()
    audit_log.append("admin_delete_moderator", request=request, target=username)
    return JsonResponse({"accounts": _moderator_accounts()})


@require_GET
@user_manager.full_moderator_required
def identity_search(request):
    query = request.GET.get("q", "").strip()
    identities = models.ClientIdentity.objects.filter(codename__icontains=query).order_by("codename")[:20] if query else models.ClientIdentity.objects.order_by("-last_seen")[:20]
    return JsonResponse({"results": [{"codename": i.codename, "ip": i.last_ip} for i in identities]})


@require_GET
@user_manager.full_moderator_required
def audit_search(request):
    query = request.GET.get("q", "").strip()
    rows = models.AuditEntry.objects.filter(Q(codename__icontains=query) | Q(ip__icontains=query) | Q(song_title__icontains=query))[:1000]
    return JsonResponse({"results": [{"ts": r.created.timestamp(), "action": r.action, "actor": r.actor, "actorRole": r.actor_role, "ip": r.ip, "codename": r.codename, "browserToken": r.browser_token, "target": r.target, "songKey": r.song_key, "songTitle": r.song_title, "metadata": r.metadata} for r in rows]})


@require_GET
@user_manager.moderator_required
def song_details(request):
    key = request.GET.get("key", "")
    if not str(key).isdigit():
        return HttpResponseBadRequest("Invalid queue key.")
    song = models.QueuedSong.objects.filter(id=int(key)).first()
    if not song:
        return HttpResponseBadRequest("Song does not exist.")
    return JsonResponse({"song": _serialize_song(song, include_details=True)})


@require_POST
@user_manager.moderator_required
def review_song(request):
    key = request.POST.get("key", "")
    if not str(key).isdigit():
        return HttpResponseBadRequest("Invalid queue key.")
    song = models.QueuedSong.objects.filter(id=int(key)).first()
    decision = request.POST.get("decision", "")
    if not song or decision not in {"hold", "approve", "deny"}:
        return HttpResponseBadRequest("Invalid song review request.")
    title = song.displayname()
    queue_key = song.id
    if decision == "deny":
        playback.queue.remove(song.id)
    else:
        song.review_status = "pending" if decision == "hold" else "approved"
        song.review_reason = "Held manually by moderator" if decision == "hold" else ""
        song.save(update_fields=["review_status", "review_reason"])
    audit_log.append(f"moderator_{decision}_song", request=request, target="queue", song_key=queue_key, song_title=title)
    playback.queue_changed.set()
    musiq.update_state()
    return JsonResponse(_state_payload(request))


@require_POST
@user_manager.moderator_required
def remove_song(request: WSGIRequest) -> HttpResponse:
    """Remove a song from the queue by queue key."""
    key = request.POST.get("key")
    if not key:
        return HttpResponseBadRequest("Missing queue key")
    try:
        removed = playback.queue.remove(int(key))
        audit_log.append(
            "moderator_remove_song",
            request=request,
            target="queue",
            song_key=int(key),
            song_title=removed.displayname(),
        )
        if not removed.manually_requested:
            playback.handle_autoplay(removed.external_url or removed.title)
        else:
            playback.handle_autoplay()
    except models.QueuedSong.DoesNotExist:
        return HttpResponseBadRequest("Song does not exist")
    musiq.update_state()
    return JsonResponse(_state_payload(request))

@require_POST
@user_manager.moderator_required
def skip_current_song(_request: WSGIRequest) -> HttpResponse:
    """Skip the currently playing song."""
    musiq_controller._skip(reason="moderator")
    audit_log.append("moderator_skip_current", request=_request, target="current-song")
    musiq.update_state()
    return JsonResponse(_state_payload(_request))

@require_POST
@user_manager.full_moderator_required
def ban_ip(request: WSGIRequest) -> HttpResponse:
    """Ban a requester IP directly or via a queue key."""
    ip = request.POST.get("ip", "")
    queue_key = request.POST.get("queue_key", "")

    if not ip and queue_key:
        try:
            ip = user_manager.get_song_requester_ip(int(queue_key))
        except ValueError:
            ip = ""

    if not ip:
        return HttpResponseBadRequest("No IP address available")

    try:
        normalized = user_manager.ban_ip(ip)
        audit_log.append(
            "moderator_ban_ip",
            request=request,
            target=normalized,
            metadata={"queueKey": queue_key or ""},
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return JsonResponse({"ip": normalized, "bannedIps": user_manager.get_banned_ips()})


@require_POST
@user_manager.full_moderator_required
def unban_ip(request: WSGIRequest) -> HttpResponse:
    """Unban a requester IP."""
    ip = request.POST.get("ip", "")
    if not ip:
        return HttpResponseBadRequest("Missing IP address")

    try:
        normalized = user_manager.unban_ip(ip)
        audit_log.append("moderator_unban_ip", request=request, target=normalized)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return JsonResponse({"ip": normalized, "bannedIps": user_manager.get_banned_ips()})

@require_POST
@user_manager.full_moderator_required
def whitelist_ip(request: WSGIRequest) -> HttpResponse:
    """Whitelist a trusted requester IP."""
    ip = request.POST.get("ip", "")
    if not ip:
        return HttpResponseBadRequest("Missing IP address")

    identity = models.ClientIdentity.objects.filter(codename__iexact=ip).first()
    if identity:
        ip = identity.last_ip
    try:
        normalized = user_manager.whitelist_ip(ip)
        audit_log.append("moderator_whitelist_ip", request=request, target=normalized)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return JsonResponse({"ip": normalized, "whitelistedIps": user_manager.get_whitelisted_ips()})


@require_POST
@user_manager.full_moderator_required
def unwhitelist_ip(request: WSGIRequest) -> HttpResponse:
    """Remove a trusted requester IP from the whitelist."""
    ip = request.POST.get("ip", "")
    if not ip:
        return HttpResponseBadRequest("Missing IP address")

    try:
        normalized = user_manager.unwhitelist_ip(ip)
        audit_log.append("moderator_unwhitelist_ip", request=request, target=normalized)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    return JsonResponse({"ip": normalized, "whitelistedIps": user_manager.get_whitelisted_ips()})

@require_POST
@user_manager.full_moderator_required
def add_blocklist(request: WSGIRequest) -> HttpResponse:
    """Upload or register a new IPv4 blocklist file."""
    try:
        blocklist = ip_screening.add_blocklist(
            name=request.POST.get("name", ""),
            separator=request.POST.get("separator", "auto"),
            entry_type=request.POST.get("entry_type", "auto"),
            uploaded_file=request.FILES.get("file"),
            source_url=request.POST.get("source_url", ""),
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    audit_log.append(
        "moderator_add_blocklist",
        request=request,
        target=blocklist["name"],
        metadata={
            "id": blocklist["id"],
            "entryCount": blocklist["entryCount"],
            "sourceKind": blocklist["sourceKind"],
        },
    )
    return JsonResponse(_state_payload(request))


@require_POST
@user_manager.full_moderator_required
def rename_blocklist(request: WSGIRequest) -> HttpResponse:
    """Rename a configured blocklist entry."""
    source_id = request.POST.get("id", "")
    new_name = request.POST.get("name", "")

    try:
        updated = ip_screening.rename_blocklist(source_id, new_name)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    audit_log.append(
        "moderator_rename_blocklist",
        request=request,
        target=updated["name"],
        metadata={"id": updated["id"]},
    )
    return JsonResponse(_state_payload(request))


@require_POST
@user_manager.full_moderator_required
def remove_blocklist(request: WSGIRequest) -> HttpResponse:
    """Remove a configured blocklist entry."""
    source_id = request.POST.get("id", "")
    if not source_id:
        return HttpResponseBadRequest("Missing blocklist id")

    try:
        removed_name = ip_screening.remove_blocklist(source_id)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    audit_log.append(
        "moderator_remove_blocklist",
        request=request,
        target=removed_name,
        metadata={"id": source_id},
    )
    return JsonResponse(_state_payload(request))

@require_POST
@user_manager.full_moderator_required
def set_site_mode(request: WSGIRequest) -> HttpResponse:
    """Switch between event, closing, and after-hours mode."""
    mode = request.POST.get("mode", "")
    if mode not in site_mode.VALID_MODES:
        return HttpResponseBadRequest("Invalid site mode")

    from core import queue_lock, voting
    from core.musiq import next_up

    with transaction.atomic():
        queue_lock.acquire()
        if mode == site_mode.CLOSING_MODE:
            pre_queue = list(playback.queue.filter(priority_tier="extra").values("id", "occurrence_id"))
            for song in pre_queue:
                user_manager.release_queue_slot_for_song(song["id"])
                next_up.clear_if_locked(song["id"])
                voting.retire(song["occurrence_id"])
            playback.queue.filter(priority_tier="extra").delete()
            playback.queue.rebalance_priorities()
        selected_mode = site_mode.set_mode(mode)
    audit_log.append("moderator_set_site_mode", request=request, target=selected_mode)

    if selected_mode == site_mode.AFTER_HOURS_MODE:
        playback.request_operator_command("pause_for_afterhours")
    else:
        playback.request_operator_command("resume_from_afterhours")

    if selected_mode == site_mode.CLOSING_MODE:
        musiq.update_state()

    return JsonResponse({"mode": selected_mode})

@require_POST
@user_manager.full_moderator_required
def set_request_settings(request: WSGIRequest) -> HttpResponse:
    """Update runtime song request controls."""

    try:
        request_cooldown_seconds = float(
            request.POST.get("request_cooldown_seconds", "")
        )
        max_song_duration_seconds = float(
            request.POST.get("max_song_duration_seconds", "")
        )
    except ValueError:
        return HttpResponseBadRequest("Request settings must be numeric.")

    if request_cooldown_seconds < 0:
        return HttpResponseBadRequest("Request cooldown cannot be negative.")
    if max_song_duration_seconds < 0:
        return HttpResponseBadRequest("Maximum song length cannot be negative.")

    # Keep values sane for accidental input mistakes without preventing normal use.
    request_cooldown_seconds = min(request_cooldown_seconds, 24 * 60 * 60)
    max_song_duration_seconds = min(max_song_duration_seconds, 24 * 60 * 60)

    storage.put("request_cooldown_seconds", request_cooldown_seconds)
    storage.put("max_song_duration_seconds", max_song_duration_seconds)

    audit_log.append(
        "moderator_set_request_settings",
        request=request,
        target="request-settings",
        metadata={
            "requestCooldownSeconds": request_cooldown_seconds,
            "maxSongDurationSeconds": max_song_duration_seconds,
        },
    )

    return JsonResponse(_state_payload(request))
