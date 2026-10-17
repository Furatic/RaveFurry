"""This module manages and counts user accesses and handles permissions."""
import colorsys
import ipaddress
import json
import random
import re
import secrets
import time
from ast import literal_eval
from functools import wraps
from pathlib import Path
from typing import Callable, Iterable, Optional

from django.conf import settings as conf
from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.contrib.sessions.models import Session
from django.core.handlers.wsgi import WSGIRequest
from django.db import transaction
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.utils import timezone

from redis.exceptions import RedisError

from core import redis

# kick users after some time without any request
from core.lights import leds
from core.settings import storage
from core.settings.storage import Privileges
from core.util import extract_value

import hashlib

INACTIVITY_PERIOD = 600
MODERATOR_GROUP_NAME = "moderator"
QUEUE_SLOT_TTL_SECONDS = 7 * 24 * 60 * 60
REQUESTER_IP_TTL_SECONDS = 7 * 24 * 60 * 60
RECENT_VOTE_WINDOW_SECONDS = 10 * 60
RECENT_VOTE_TTL_SECONDS = RECENT_VOTE_WINDOW_SECONDS + 60
MAX_RECENT_DOWNVOTE_TRANSITIONS = 2
MIN_RECENT_UPVOTE_TRANSITIONS = 4

SELF_REMOVE_WINDOW_SECONDS = 10 * 60
SELF_REMOVE_TTL_SECONDS = SELF_REMOVE_WINDOW_SECONDS + 60
MAX_SELF_REMOVE_ACTIONS = 5

BUILTIN_CREDENTIALS_PATH = Path(conf.BASE_DIR) / "config" / "builtin_credentials.json"
BUILTIN_PASSWORD_BYTES = 20
CLIENT_TOKEN_COOKIE = "furatic_browser"
CODENAME_WORDS = (
    "EMBER", "NOVA", "COMET", "QUARTZ", "ORBIT", "RIVER", "CINDER", "AURORA",
    "ECHO", "SOLAR", "FABLE", "GLINT", "ASTRA", "LYRIC", "POLAR", "VELVET",
    "MAPLE", "CLOUD", "PIXEL", "HARBOR", "TOPAZ", "MINT", "THUNDER", "SATIN",
    "ROCKET", "BLOSSOM", "ONYX", "LAGOON", "SPARROW", "CANYON", "JETSTREAM",
    "MERIDIAN", "CLOVE", "CASCADE", "SUMMIT", "TANGO", "MIRAGE", "DUSK", "PRISM",
    "CYPRESS", "NEBULA", "RADIANT", "FROST", "SAFFRON", "DRIFT", "TEMPEST",
    "WILLOW", "EMBERGLOW",
)


def _load_builtin_credentials() -> dict:
    try:
        with BUILTIN_CREDENTIALS_PATH.open("r", encoding="utf-8") as infile:
            data = json.load(infile)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def _save_builtin_credentials(credentials: dict) -> None:
    BUILTIN_CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with BUILTIN_CREDENTIALS_PATH.open("w", encoding="utf-8") as outfile:
        json.dump(credentials, outfile, indent=2, sort_keys=True)
        outfile.write("\n")
    try:
        BUILTIN_CREDENTIALS_PATH.chmod(0o600)
    except OSError:
        pass


def _persistent_builtin_password(username: str) -> str:
    credentials = _load_builtin_credentials()
    password = str(credentials.get(username, "")).strip()
    if not password:
        password = secrets.token_hex(BUILTIN_PASSWORD_BYTES)
        credentials[username] = password
        _save_builtin_credentials(credentials)
    return password


def _normalize_ip(value: str) -> str:
    """Normalize an IPv4/IPv6 address and drop any port / wrapper syntax."""
    if not value:
        return ""

    value = value.strip().strip('"').strip("'")
    if not value:
        return ""

    if value.lower().startswith("for="):
        value = value[4:]

    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]

    if "%" in value:
        value = value.split("%", 1)[0]

    if value.count(":") == 1 and "." in value:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            value = host

    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return ""

def normalize_ip(value: str) -> str:
    """Public wrapper for normalized client/requester IP comparisons."""
    return _normalize_ip(value)

def _normalize_ip_collection(values: Iterable[str]) -> list[str]:
    normalized = {_normalize_ip(value) for value in values}
    normalized.discard("")
    return sorted(normalized)


def _parse_forwarded_header(value: str) -> str:
    if not value:
        return ""
    for candidate in value.split(","):
        normalized = _normalize_ip(candidate)
        if normalized:
            return normalized
    return ""


def _parse_rfc_forwarded_header(value: str) -> str:
    if not value:
        return ""
    matches = re.findall(
        r'for=(?:"?)(\[[^\]]+\]|[^;,"\s]+)',
        value,
        flags=re.IGNORECASE,
    )
    for candidate in matches:
        normalized = _normalize_ip(candidate)
        if normalized:
            return normalized
    return ""


def _extract_forwarded_ip(request: WSGIRequest) -> str:
    for header_name in conf.CLIENT_IP_HEADER_CANDIDATES:
        raw_value = request.META.get(header_name, "")
        if not raw_value:
            continue
        if header_name == "HTTP_FORWARDED":
            normalized = _parse_rfc_forwarded_header(raw_value)
        else:
            normalized = _parse_forwarded_header(raw_value)
        if normalized:
            return normalized
    return ""


def _trusted_proxy(remote_addr: str) -> bool:
    normalized = _normalize_ip(remote_addr)
    if not normalized:
        return False

    remote_ip = ipaddress.ip_address(normalized)

    for trusted in conf.TRUSTED_PROXY_IPS:
        trusted = str(trusted).strip()
        if not trusted:
            continue

        try:
            if "/" in trusted:
                if remote_ip in ipaddress.ip_network(trusted, strict=False):
                    return True
            else:
                if remote_ip == ipaddress.ip_address(trusted):
                    return True
        except ValueError:
            continue

    return False


def _resolve_client_ip(request: WSGIRequest) -> str:
    direct_ip = _normalize_ip(request.META.get("REMOTE_ADDR", ""))

    if direct_ip and _trusted_proxy(direct_ip):
        forwarded_ip = _extract_forwarded_ip(request)
        if forwarded_ip:
            return forwarded_ip

    return direct_ip


def has_controls(user) -> bool:
    """Determines whether the given user is allowed to control playback."""
    return is_admin(user)


def is_admin(user) -> bool:
    """Determines whether the given user is the admin."""
    return bool(getattr(user, "is_authenticated", False) and getattr(user, "is_active", False) and getattr(user, "is_superuser", False))


def is_moderator(user) -> bool:
    """Determines whether the given user belongs to the moderator role."""
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    return bool(user.groups.filter(name=MODERATOR_GROUP_NAME).exists())


def can_moderate(user) -> bool:
    """Determines whether the given user can access moderator tools."""
    return is_admin(user) or is_moderator(user)


def is_song_only_moderator(user) -> bool:
    if is_admin(user) or not is_moderator(user):
        return False
    try:
        return bool(user.moderatorprofile.song_only)
    except Exception:  # profile-less legacy moderators retain full access
        return False


def full_moderator_required(func):
    """Require admin or a non-song-only moderator."""
    @moderator_required
    def wrapped(request, *args, **kwargs):
        if is_song_only_moderator(request.user):
            return JsonResponse({"message": "Full moderator access is required."}, status=403)
        return func(request, *args, **kwargs)
    return wraps(func)(wrapped)


def admin_required(func):
    """Require the administrator account."""
    @moderator_required
    def wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            return JsonResponse({"message": "Administrator access is required."}, status=403)
        return func(request, *args, **kwargs)
    return wraps(func)(wrapped)


def client_identity(request, create: bool = True):
    """Resolve the unique random browser cookie to its server-side codename."""
    from core.models import ClientIdentity
    raw = str(getattr(request, "furatic_browser_token", "") or request.COOKIES.get(CLIENT_TOKEN_COOKIE, ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", raw):
        if not create:
            return None
        raw = secrets.token_urlsafe(32)
        request.furatic_browser_token = raw
        request.furatic_set_browser_cookie = True
    else:
        # Audit entries must use the real token belonging to this request. The
        # previous implementation only set this attribute for brand-new users,
        # which made every returning user's token appear blank in moderation.
        request.furatic_browser_token = raw
    digest = hashlib.sha256(raw.encode()).hexdigest()
    identity = ClientIdentity.objects.filter(token_hash=digest).first()
    ip = get_client_ip(request)
    if identity is None and create:
        word = CODENAME_WORDS[int(digest[:8], 16) % len(CODENAME_WORDS)]
        base = f"{word}-{(int(digest[8:16], 16) % 97) + 1:02d}"
        codename = base
        suffix = 1
        while ClientIdentity.objects.filter(codename=codename).exists():
            suffix += 1
            codename = f"{base}-{suffix}"
        identity = ClientIdentity.objects.create(token_hash=digest, codename=codename, first_ip=ip, last_ip=ip)
    elif identity:
        now = timezone.now()
        updates = {}
        if ip and identity.last_ip != ip:
            identity.last_ip = ip
            updates["last_ip"] = ip
        if (now - identity.last_seen).total_seconds() >= 300 or updates:
            updates["last_seen"] = now
            ClientIdentity.objects.filter(pk=identity.pk).update(**updates)
    request.furatic_identity = identity
    return identity


def moderator_required(
    func: Callable[[WSGIRequest], HttpResponse]
) -> Callable[[WSGIRequest], HttpResponse]:
    """Require an authenticated moderator or admin for the wrapped view."""

    def _decorator(request: WSGIRequest, *args, **kwargs) -> HttpResponse:
        is_ajax_request = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.path.startswith("/api/")
            or request.path.startswith("/ajax/")
        )

        if not getattr(request, "user", None) or not request.user.is_authenticated:
            if is_ajax_request:
                return JsonResponse(
                    {
                        "loginRequired": True,
                        "loginUrl": "/moderator-login/",
                        "message": "Your moderator session has expired.",
                    },
                    status=401,
                )

            return redirect_to_login(
                request.get_full_path(),
                login_url="/moderator-login/",
            )

        if not can_moderate(request.user):
            if is_ajax_request:
                return JsonResponse(
                    {
                        "loginRequired": True,
                        "loginUrl": "/moderator-login/",
                        "message": "Moderator access is required.",
                    },
                    status=403,
                )

            return HttpResponseForbidden("Moderator access required")

        return func(request, *args, **kwargs)

    return wraps(func)(_decorator)


def has_secret_controls(request: WSGIRequest) -> bool:
    """Determines whether this session was unlocked via the secret full-control route."""
    return bool(
        getattr(request, "session", None) and request.session.get("secret_controls")
    )


def has_privilege(user, privilege: Privileges):
    if privilege == Privileges.everybody:
        return True
    if privilege == Privileges.mod and can_moderate(user):
        return True
    if privilege == Privileges.admin and is_admin(user):
        return True
    return False


def ensure_builtin_moderator(
    password: Optional[str] = None, rotate_if_unset: bool = False
) -> tuple[str, str]:
    """Create/update the built-in moderator account.

    Avoid rehashing the same password on every restart, because Django auth
    sessions include the password hash and would be invalidated.
    """
    from django.contrib.auth.models import Group

    UserModel = get_user_model()
    username = conf.FURATIC_MOD_USERNAME
    configured_password = password
    if configured_password is None:
        configured_password = getattr(conf, "FURATIC_MOD_PASSWORD", "")
    configured_password = (configured_password or "").strip()

    returned_password = ""
    password_to_apply = ""

    group, _ = Group.objects.get_or_create(name=MODERATOR_GROUP_NAME)
    user, _ = UserModel.objects.get_or_create(
        **{UserModel.USERNAME_FIELD: username}
    )

    # Account access is managed by the administrator; retain a suspension
    # across routine startups instead of silently re-enabling this account.
    if hasattr(user, "is_staff"):
        user.is_staff = False
    if hasattr(user, "is_superuser"):
        user.is_superuser = False

    if configured_password:
        password_to_apply = configured_password
        returned_password = configured_password if password is not None else ""
    elif rotate_if_unset:
        password_to_apply = _persistent_builtin_password(username)
        returned_password = password_to_apply

    if password_to_apply and not user.check_password(password_to_apply):
        user.set_password(password_to_apply)

    user.save()
    user.groups.add(group)

    return username, returned_password

def ensure_builtin_admin(
    password: Optional[str] = None,
    rotate_if_unset: bool = False,
) -> tuple[str, str]:
    """Create/update the built-in admin account and optionally persist its password.

    Avoid rehashing the same password on every restart, because Django auth
    sessions include the password hash and would be invalidated.
    """

    UserModel = get_user_model()
    username = "admin"

    configured_password = (password or "").strip()
    returned_password = ""
    password_to_apply = ""

    user, _ = UserModel.objects.get_or_create(
        **{UserModel.USERNAME_FIELD: username},
        defaults={
            "is_staff": True,
            "is_superuser": True,
            "is_active": True,
        },
    )

    user.is_staff = True
    user.is_superuser = True
    user.is_active = True

    if configured_password:
        password_to_apply = configured_password
        returned_password = configured_password
    elif rotate_if_unset:
        password_to_apply = _persistent_builtin_password(username)
        returned_password = password_to_apply

    if password_to_apply and not user.check_password(password_to_apply):
        user.set_password(password_to_apply)

    user.save(update_fields=["password", "is_staff", "is_superuser", "is_active"])

    return username, returned_password


def _banned_ips_storage_key() -> str:
    return str(storage.get("banned_ips"))


def get_banned_ips() -> list[str]:
    """Return the persisted banned IP list."""
    raw_value = _banned_ips_storage_key()
    return _normalize_ip_collection(re.split(r"[\s,]+", raw_value))


def _store_banned_ips(ips: Iterable[str]) -> None:
    storage.put("banned_ips", "\n".join(_normalize_ip_collection(ips)))


def is_banned_ip(ip: str) -> bool:
    normalized = _normalize_ip(ip)
    return bool(normalized and normalized in set(get_banned_ips()))


def ban_ip(ip: str) -> str:
    """Persistently ban the given IP and return the normalized value."""
    normalized = _normalize_ip(ip)
    if not normalized:
        raise ValueError("Invalid IP address")
    banned_ips = set(get_banned_ips())
    banned_ips.add(normalized)
    _store_banned_ips(banned_ips)
    return normalized


def unban_ip(ip: str) -> str:
    """Remove the given IP from the persistent ban list."""
    normalized = _normalize_ip(ip)
    if not normalized:
        raise ValueError("Invalid IP address")
    banned_ips = set(get_banned_ips())
    banned_ips.discard(normalized)
    _store_banned_ips(banned_ips)
    return normalized


def _whitelisted_ips_storage_key() -> str:
    return str(storage.get("whitelisted_ips"))


def get_whitelisted_ips() -> list[str]:
    """Return the persisted whitelist of trusted IPs."""
    raw_value = _whitelisted_ips_storage_key()
    return _normalize_ip_collection(re.split(r"[\s,]+", raw_value))


def _store_whitelisted_ips(ips: Iterable[str]) -> None:
    storage.put("whitelisted_ips", "\n".join(_normalize_ip_collection(ips)))


def is_whitelisted_ip(ip: str) -> bool:
    normalized = _normalize_ip(ip)
    return bool(normalized and normalized in set(get_whitelisted_ips()))


def whitelist_ip(ip: str) -> str:
    """Persistently whitelist the given IP and return the normalized value."""
    normalized = _normalize_ip(ip)
    if not normalized:
        raise ValueError("Invalid IP address")
    whitelisted_ips = set(get_whitelisted_ips())
    whitelisted_ips.add(normalized)
    _store_whitelisted_ips(whitelisted_ips)
    return normalized


def unwhitelist_ip(ip: str) -> str:
    """Remove the given IP from the persistent whitelist."""
    normalized = _normalize_ip(ip)
    if not normalized:
        raise ValueError("Invalid IP address")
    whitelisted_ips = set(get_whitelisted_ips())
    whitelisted_ips.discard(normalized)
    _store_whitelisted_ips(whitelisted_ips)
    return normalized


def normalize_session_key(session_key: str = "") -> str:
    """Return a short stable session identifier safe for database fields and Redis keys."""

    raw_session_key = str(session_key or "").strip()
    if not raw_session_key:
        return ""

    if len(raw_session_key) <= 50:
        return raw_session_key

    return hashlib.sha256(raw_session_key.encode("utf-8")).hexdigest()[:50]

def _queue_slot_key(request_ip: str) -> str:
    return f"queue-slot:{request_ip}"


def _requester_ip_key(queue_key: int) -> str:
    return f"queue-requester:{queue_key}"


def remember_requester_ip(
    request_ip: str,
    queue_key: int,
    session_key: str = "",
) -> None:
    """Store requester IP metadata for ownership, moderation, and exports."""
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return

    try:
        redis.connection.set(
            _requester_ip_key(queue_key),
            normalized,
            ex=REQUESTER_IP_TTL_SECONDS,
        )
    except RedisError as error:
        logger.warning("failed to remember requester IP in redis for %s: %s", queue_key, error)

    try:
        from core import models

        updates = {"requester_ip": normalized}
        normalized_session_key = normalize_session_key(session_key)
        if normalized_session_key:
            updates["requester_session_key"] = normalized_session_key

        models.QueuedSong.objects.filter(id=queue_key).update(**updates)
        models.CurrentSong.objects.filter(queue_key=queue_key).update(**updates)
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to remember requester IP in database for %s: %s", queue_key, error)

def get_song_requester_ip(queue_key: int) -> str:
    """Return the requester IP stored for the given queue / current-song key."""
    try:
        requester_ip = redis.connection.get(_requester_ip_key(queue_key)) or ""
        if requester_ip:
            return requester_ip
    except RedisError as error:
        logger.warning("failed to read requester IP from redis for %s: %s", queue_key, error)

    try:
        from core import models

        requester_ip = (
            models.QueuedSong.objects.filter(id=queue_key)
            .values_list("requester_ip", flat=True)
            .first()
            or ""
        )

        if not requester_ip:
            requester_ip = (
                models.CurrentSong.objects.filter(queue_key=queue_key)
                .values_list("requester_ip", flat=True)
                .first()
                or ""
            )

        requester_ip = _normalize_ip(requester_ip)
        if requester_ip:
            try:
                redis.connection.set(
                    _requester_ip_key(queue_key),
                    requester_ip,
                    ex=REQUESTER_IP_TTL_SECONDS,
                )
            except RedisError:
                pass
        return requester_ip
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to read requester IP from database for %s: %s", queue_key, error)
        return ""

def song_belongs_to_ip(request_ip: str, queue_key: int) -> bool:
    """Return whether the given queue entry belongs to the given requester IP."""
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return False

    return get_song_requester_ip(queue_key) == normalized


def _self_remove_key(request_ip: str) -> str:
    return f"self-removes:{request_ip}"


def can_self_remove_song(request_ip: str) -> bool:
    """Allow only a few self-removals within the configured time window."""
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return False

    now = time.time()
    cutoff = now - SELF_REMOVE_WINDOW_SECONDS
    key = _self_remove_key(normalized)

    try:
        pipe = redis.connection.pipeline()
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.zcount(key, cutoff, "+inf")
        pipe.expire(key, SELF_REMOVE_TTL_SECONDS)
        _, count, _ = pipe.execute()
        return int(count) < MAX_SELF_REMOVE_ACTIONS
    except RedisError as error:
        logger.warning("failed to check self-remove limit for %s: %s", normalized, error)
        return False


def record_self_remove(request_ip: str, queue_key: int) -> None:
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return

    now = time.time()
    cutoff = now - SELF_REMOVE_WINDOW_SECONDS
    key = _self_remove_key(normalized)
    member = f"{now}:{queue_key}"

    try:
        pipe = redis.connection.pipeline()
        pipe.zadd(key, {member: now})
        pipe.zremrangebyscore(key, "-inf", cutoff)
        pipe.expire(key, SELF_REMOVE_TTL_SECONDS)
        pipe.execute()
    except RedisError as error:
        logger.warning("failed to record self-remove for %s: %s", normalized, error)

def get_active_queue_slot(request_ip: str) -> Optional[int]:
    """Return the active queued-song id owned by this IP, if any."""
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return None

    try:
        value = redis.connection.get(_queue_slot_key(normalized))
    except RedisError as error:
        logger.warning("failed to read queue slot for %s: %s", normalized, error)
        return None

    if not value:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ip_has_active_queue_slot(request_ip: str) -> bool:
    """Return whether the given IP currently owns a queued song slot."""
    return get_active_queue_slot(request_ip) is not None


def claim_queue_slot(request_ip: str, queue_key: int) -> bool:
    """Claim the single queued-song slot for the given IP.

    Returns False if the IP already has another song in the queue.

    """
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return True

    remember_requester_ip(normalized, queue_key)

    slot_key = _queue_slot_key(normalized)
    requester_key = _requester_ip_key(queue_key)

    allowed = True

    def check_entry(pipe) -> None:
        nonlocal allowed
        existing = pipe.get(slot_key)
        if existing not in (None, "", str(queue_key)):
            allowed = False
            return

        allowed = True
        pipe.multi()
        pipe.set(slot_key, queue_key, ex=QUEUE_SLOT_TTL_SECONDS)
        pipe.set(requester_key, normalized, ex=REQUESTER_IP_TTL_SECONDS)

    try:
        redis.connection.transaction(check_entry, slot_key)
        return allowed
    except RedisError as error:
        logger.warning("failed to claim queue slot for %s: %s", normalized, error)
        return True


def release_queue_slot_for_song(queue_key: int) -> None:
    """Release the active queue slot owned by the requester of the given song."""
    requester_ip = get_song_requester_ip(queue_key)
    if not requester_ip:
        return

    slot_key = _queue_slot_key(requester_ip)

    try:
        current_value = redis.connection.get(slot_key)

        pipe = redis.connection.pipeline()
        if current_value == str(queue_key):
            pipe.delete(slot_key)
        pipe.expire(_requester_ip_key(queue_key), REQUESTER_IP_TTL_SECONDS)
        pipe.execute()
    except RedisError as error:
        logger.warning("failed to release queue slot for %s: %s", queue_key, error)


def clear_queue_slots() -> None:
    """Clear all active single-song queue ownership locks."""
    try:
        keys = list(redis.connection.scan_iter(match="queue-slot:*"))
        if keys:
            redis.connection.delete(*keys)
    except RedisError as error:
        logger.warning("failed to clear queue slots: %s", error)

def _request_cooldown_key(request_ip: str, session_key: str = "") -> str:
    normalized_ip = _normalize_ip(request_ip)
    if normalized_ip:
        return f"request-cooldown:ip:{normalized_ip}"

    clean_session_key = normalize_session_key(session_key)
    if clean_session_key:
        return f"request-cooldown:session:{clean_session_key}"

    return ""


def request_cooldown_remaining(request_ip: str, session_key: str = "") -> int:
    """Return remaining request cooldown seconds for this requester.

    Returns 0 when cooldown is disabled, the requester cannot be identified,
    or Redis is unavailable. Redis failure should not break song requests.
    """

    cooldown_seconds = int(max(0, round(float(storage.get("request_cooldown_seconds")))))
    if cooldown_seconds <= 0:
        return 0

    key = _request_cooldown_key(request_ip, session_key)
    if not key:
        return 0

    try:
        ttl = redis.connection.ttl(key)
    except RedisError as error:
        logger.warning("failed to read request cooldown for %s: %s", key, error)
        return 0

    if ttl is None or ttl < 0:
        return 0

    return int(ttl)


def remember_request_cooldown(request_ip: str, session_key: str = "") -> None:
    """Start the request cooldown for this requester."""

    cooldown_seconds = int(max(0, round(float(storage.get("request_cooldown_seconds")))))
    if cooldown_seconds <= 0:
        return

    key = _request_cooldown_key(request_ip, session_key)
    if not key:
        return

    try:
        redis.connection.set(key, "1", ex=cooldown_seconds)
    except RedisError as error:
        logger.warning("failed to store request cooldown for %s: %s", key, error)

def clear_request_cooldown(request_ip: str, session_key: str = "") -> None:
    """Clear request cooldown state for a failed/removed placeholder request."""

    keys = []

    normalized_ip = _normalize_ip(request_ip)
    if normalized_ip:
        keys.append(f"request-cooldown:ip:{normalized_ip}")

    clean_session_key = normalize_session_key(session_key)
    if clean_session_key:
        keys.append(f"request-cooldown:session:{clean_session_key}")

    if not keys:
        return

    try:
        redis.connection.delete(*keys)
    except RedisError as error:
        logger.warning("failed to clear request cooldown for %s: %s", keys, error)

def update_user_count() -> None:
    """Go through all recent requests and delete those that were too long ago."""
    now = time.time()
    last_requests = redis.get("last_requests")
    for key, value in list(last_requests.items()):
        if now - value >= INACTIVITY_PERIOD:
            del last_requests[key]
            redis.put("last_requests", last_requests)
    redis.put("last_user_count_update", now)


def get_count() -> int:
    """Returns the number of currently active users.
    Updates this number after an intervals since the last update."""
    if time.time() - redis.get("last_user_count_update") >= 60:
        update_user_count()
    return len(redis.get("last_requests"))


def partymode_enabled() -> bool:
    """Determines whether partymode is enabled,
    based on the number of currently active users."""
    return len(redis.get("last_requests")) >= storage.get("people_to_party")


import logging

logger = logging.getLogger(__name__)

from typing import Any, Callable, Iterable, Mapping, Optional


def _extract_forwarded_ip(meta: Mapping[str, str]) -> str:
    for header_name in conf.CLIENT_IP_HEADER_CANDIDATES:
        raw_value = meta.get(header_name, "")
        if not raw_value:
            continue
        if header_name == "HTTP_FORWARDED":
            normalized = _parse_rfc_forwarded_header(raw_value)
        else:
            normalized = _parse_forwarded_header(raw_value)
        if normalized:
            return normalized
    return ""


def _resolve_client_ip_from_meta(meta: Mapping[str, str], remote_addr: str) -> str:
    direct_ip = _normalize_ip(remote_addr)

    if direct_ip and _trusted_proxy(direct_ip):
        forwarded_ip = _extract_forwarded_ip(meta)
        if forwarded_ip:
            return forwarded_ip

    return direct_ip


def get_client_ip(request: WSGIRequest) -> str:
    return _resolve_client_ip_from_meta(
        request.META,
        request.META.get("REMOTE_ADDR", ""),
    ) or ""


def get_client_ip_from_scope(scope: Mapping[str, Any]) -> str:
    meta: dict[str, str] = {}

    for key, value in scope.get("headers", []):
        if isinstance(key, bytes):
            key = key.decode("latin1")
        if isinstance(value, bytes):
            value = value.decode("latin1")
        meta["HTTP_" + key.upper().replace("-", "_")] = value

    client = scope.get("client")
    remote_addr = ""
    if client and len(client) >= 1:
        remote_addr = str(client[0])

    return _resolve_client_ip_from_meta(meta, remote_addr) or ""


def _recent_downvote_key(request_ip: str) -> str:
    return f"recent-downvotes:{request_ip}"


def _recent_upvote_key(request_ip: str) -> str:
    return f"recent-upvotes:{request_ip}"


def _trim_recent_vote_activity(request_ip: str, now: float) -> tuple[str, str, float]:
    down_key = _recent_downvote_key(request_ip)
    up_key = _recent_upvote_key(request_ip)
    cutoff = now - RECENT_VOTE_WINDOW_SECONDS

    pipe = redis.connection.pipeline()
    pipe.zremrangebyscore(down_key, "-inf", cutoff)
    pipe.zremrangebyscore(up_key, "-inf", cutoff)
    pipe.expire(down_key, RECENT_VOTE_TTL_SECONDS)
    pipe.expire(up_key, RECENT_VOTE_TTL_SECONDS)
    pipe.execute()

    return down_key, up_key, cutoff


def _can_add_recent_downvote(
    request_ip: str, queue_key: int, previous_vote: int, now: float
) -> bool:
    down_key, up_key, cutoff = _trim_recent_vote_activity(request_ip, now)
    recent_downvotes = redis.connection.zcount(down_key, cutoff, "+inf")
    recent_upvotes = redis.connection.zcount(up_key, cutoff, "+inf")

    # If this vote flips a recent upvote on the same song into a downvote,
    # that song should no longer count as balancing upvote activity.
    if previous_vote > 0 and redis.connection.zscore(up_key, str(queue_key)) is not None:
        recent_upvotes -= 1

    return (
        recent_downvotes < MAX_RECENT_DOWNVOTE_TRANSITIONS
        or recent_upvotes >= MIN_RECENT_UPVOTE_TRANSITIONS
    )


def _sync_recent_vote_activity(
    request_ip: str, queue_key: int, new_vote: int, now: float, record_activity: bool
) -> None:
    if not record_activity:
        return

    down_key, up_key, cutoff = _trim_recent_vote_activity(request_ip, now)
    member = str(queue_key)
    pipe = redis.connection.pipeline()

    if new_vote < 0:
        pipe.zadd(down_key, {member: now})
        pipe.zrem(up_key, member)
    elif new_vote > 0:
        pipe.zadd(up_key, {member: now})
        pipe.zrem(down_key, member)
    else:
        pipe.zrem(down_key, member)
        pipe.zrem(up_key, member)

    pipe.zremrangebyscore(down_key, "-inf", cutoff)
    pipe.zremrangebyscore(up_key, "-inf", cutoff)
    pipe.expire(down_key, RECENT_VOTE_TTL_SECONDS)
    pipe.expire(up_key, RECENT_VOTE_TTL_SECONDS)
    pipe.execute()


def try_vote(
    request_ip: str, queue_key: int, amount: int, record_activity: bool = True
) -> bool:
    """If the user can not vote any more for the song into the given direction, return False.
    Otherwise, perform the vote and returns True."""
    normalized = _normalize_ip(request_ip)
    if not normalized:
        return True

    # Votes are stored as individual (who, what) tuples in redis.
    # A mapping who -> [what, ...] is not used,
    # because each modification would require deserialization and subsequent serialization.
    # Without such a mapping we cannot easily find all votes belonging to a session key,
    # which would be required to update the view of a user whose client-votes got desynced.
    # This should never happen during normal usage, so we optimize for our main use case:
    # looking up whether a single user voted for a single song, which is constant with tuples.
    # Since this feature indexes by the request IP and not the session_key,
    # it can not share its data structure with the votes for the color indicators.
    entry = str((normalized, queue_key))
    timestamp_key = f"vote-ts:{normalized}:{queue_key}"
    allowed = True
    cooldown_seconds = float(storage.get("vote_change_cooldown_seconds"))
    now = time.time()
    previous_vote = 0
    new_vote = 0

    # redis transaction: https://github.com/Redis/redis-py#pipelines
    def check_entry(pipe) -> None:
        nonlocal allowed, previous_vote, new_vote
        vote = pipe.get(entry)
        last_change = pipe.get(timestamp_key)

        if (
            cooldown_seconds > 0
            and last_change is not None
            and now - float(last_change) < cooldown_seconds
        ):
            allowed = False
            return

        previous_vote = 0 if vote is None else int(vote)
        new_vote = previous_vote + amount

        if new_vote < -1 or new_vote > 1:
            allowed = False
            return

        # Only limit transitions into a real downvote. Upvotes and clearing votes stay untouched.
        if (
            record_activity
            and previous_vote >= 0
            and new_vote < 0
            and not _can_add_recent_downvote(normalized, queue_key, previous_vote, now)
        ):
            allowed = False
            return

        allowed = True
        # expire these entries to avoid accumulation over long runtimes.
        pipe.multi()
        pipe.set(entry, new_vote, ex=24 * 60 * 60)
        pipe.set(timestamp_key, now, ex=24 * 60 * 60)

    redis.connection.transaction(check_entry, entry, timestamp_key)

    if allowed:
        _sync_recent_vote_activity(
            normalized, queue_key, new_vote, now, record_activity
        )

    return allowed


def _get_next_color():
    with transaction.atomic():
        next_index = storage.get("next_color_index")
        storage.put("next_color_index", next_index + 1)

    offset = storage.get("color_offset")

    hue = offset + next_index * (137.508 / 360)  # approximation for the golden angle
    color = colorsys.hsv_to_rgb(hue, 0.4, 1)

    return "#%02x%02x%02x" % tuple(round(v * 255) for v in color)


def color_of(session_key: str) -> Optional[str]:
    if not session_key or storage.get("color_indication") == storage.Privileges.nobody:
        return None
    # no transaction because this is called many times and at worst race conditions would result
    # in a different color being set
    color = redis.connection.get("color-" + session_key)
    if color is None:
        color = _get_next_color()
    # TODO: this is lost on server restart.
    # maybe store the color client side so it can be recovered?
    redis.connection.set("color-" + session_key, color, ex=24 * 60 * 60)
    return color


def register_song(request: WSGIRequest, queue_key: int) -> None:
    # For each song, identified by its queue_key, the following information is stored:
    # (<session_key>, {<session_key>: <vote>, …})
    # This requires a session_key, thus it can only be used in @tracked functions
    session_key = request.session.session_key

    key = f"engagement-{queue_key}"

    def update_entry(pipe) -> None:
        value = pipe.get(key)
        engagement = None if value is None else literal_eval(value)
        if engagement is None:
            engagement = (None, {})
        _, votes = engagement
        # expire these entries to avoid accumulation over long runtimes.
        pipe.multi()
        pipe.set(key, str((session_key, votes)), ex=24 * 60 * 60)

    redis.connection.transaction(update_entry, key)


def register_vote(request: WSGIRequest, queue_key: int, amount: int) -> None:
    session_key = request.session.session_key

    key = f"engagement-{queue_key}"

    def update_entry(pipe) -> None:
        value = pipe.get(key)
        engagement = None if value is None else literal_eval(value)
        if engagement is None:
            engagement = (None, {})
        requested_by, votes = engagement
        if session_key in votes:
            votes[session_key] += amount
        else:
            votes[session_key] = amount
        # clamp votes to [-1,1]. This helps recovering from desyncs after redis was cleared
        # but client votes are still locked in
        votes[session_key] = max(-1, min(1, votes[session_key]))
        if votes[session_key] == 0:
            del votes[session_key]
        # expire these entries to avoid accumulation over long runtimes.
        pipe.multi()
        pipe.set(key, str((requested_by, votes)), ex=24 * 60 * 60)

    redis.connection.transaction(update_entry, key)


def set_user_color(request: WSGIRequest) -> HttpResponse:
    """Updates the color assigned to the session of the request."""
    from core.musiq import musiq

    color, response = extract_value(request.POST)
    if not color or not re.match(r"^#[0-9a-f]{6}$", color):
        return HttpResponseBadRequest()
    session_key = request.session.session_key
    redis.connection.set("color-" + session_key, color, ex=24 * 60 * 60)
    musiq.update_state()
    return response


def tracked(
    func: Callable[[WSGIRequest], HttpResponse]
) -> Callable[[WSGIRequest], HttpResponse]:
    """A decorator that stores the last access for every connected ip
    so the number of active users can be determined."""

    def _decorator(request: WSGIRequest) -> HttpResponse:
        # create a sessions if none exists (necessary for anonymous users)
        if not request.session or not request.session.session_key:
            # if there are no active sessions (= this is the first one)
            # reset the color index and choose a new offset.
            active_sessions = Session.objects.filter(
                expire_date__gte=timezone.now()
            ).count()
            if active_sessions == 0:
                storage.put("color_offset", random.random())
                storage.put("next_color_index", 0)

            # Signed-cookie sessions encode their contents. Saving an empty session
            # produces the same deterministic key for every anonymous browser.
            # Persist a random per-browser value so requester session IDs are real.
            request.session["browser_token"] = secrets.token_urlsafe(24)
            request.session.save()

        request_ip = get_client_ip(request)
        activity_key = request_ip or request.session.session_key or "anonymous"
        last_requests = redis.get("last_requests")
        last_requests[activity_key] = time.time()
        redis.put("last_requests", last_requests)

        def check():
            active = redis.get("active_requests")
            if active > 0:
                leds.enable_act_led()
            else:
                leds.disable_act_led()

        try:
            redis.connection.incr("active_requests")
            check()
            return func(request)
        finally:
            try:
                redis.connection.decr("active_requests")
                check()
            except RedisError as error:
                logger.warning("failed to finish tracked request cleanup: %s", error)

    return wraps(func)(_decorator)
