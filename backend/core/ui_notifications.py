"""Small event-page notification bus for public FURATIC UI updates."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from django.core.handlers.wsgi import WSGIRequest
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from redis.exceptions import RedisError

from core import redis, user_manager

NOTIFICATION_LIST_KEY = "furatic-ui-notifications"
NOTIFICATION_SEQUENCE_KEY = "furatic-ui-notifications-seq"
MAX_STORED_NOTIFICATIONS = 80


def emit(
    kind: str,
    title: str,
    message: str,
    *,
    level: str = "info",
    icon: str = "✨",
    recipient_token: str = "",
    request_key: int | None = None,
) -> None:
    """Publish a short-lived public UI notification.

    Notifications are intentionally stored in Redis, not the database. They are
    transient event UI messages and should not survive a full Redis reset.
    """
    try:
        sequence = int(redis.connection.incr(NOTIFICATION_SEQUENCE_KEY))
        payload = {
            "id": sequence,
            "kind": kind,
            "title": title,
            "message": message,
            "level": level,
            "icon": icon,
            "createdAt": time.time(),
            "recipientToken": recipient_token,
            "requestKey": request_key,
        }
        redis.connection.rpush(NOTIFICATION_LIST_KEY, json.dumps(payload))
        redis.connection.ltrim(
            NOTIFICATION_LIST_KEY,
            -MAX_STORED_NOTIFICATIONS,
            -1,
        )
    except RedisError:
        return


def _recent_after(after: int, recipient_token: str = "") -> List[Dict[str, Any]]:
    try:
        raw_items = redis.connection.lrange(NOTIFICATION_LIST_KEY, 0, -1)
    except RedisError:
        return []

    notifications: List[Dict[str, Any]] = []
    for raw_item in raw_items:
        try:
            item = json.loads(raw_item)
        except (TypeError, ValueError):
            continue

        if not isinstance(item, dict):
            continue

        recipient = item.get("recipientToken", "")
        if recipient and recipient != recipient_token:
            continue

        try:
            item_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue

        if item_id <= after:
            continue

        notifications.append(
            {
                "id": item_id,
                "kind": str(item.get("kind", "info")),
                "title": str(item.get("title", ""))[:120],
                "message": str(item.get("message", ""))[:260],
                "level": str(item.get("level", "info")),
                "icon": str(item.get("icon", "✨"))[:8],
                "createdAt": float(item.get("createdAt", 0.0) or 0.0),
                "requestKey": item.get("requestKey"),
            }
        )

    notifications.sort(key=lambda item: int(item["id"]))
    private = [item for item in notifications if item["kind"].startswith("song_request_")]
    public = [item for item in notifications if not item["kind"].startswith("song_request_")][-12:]
    return sorted(private + public, key=lambda item: int(item["id"]))


@require_GET
def notifications(request: WSGIRequest) -> JsonResponse:
    """Return recent event-page notifications newer than the given sequence id."""
    try:
        after = int(request.GET.get("after", "0"))
    except (TypeError, ValueError):
        after = 0

    identity = user_manager.client_identity(request, create=False)
    items = _recent_after(after, identity.token_hash if identity else "")
    latest = after
    if items:
        latest = max(int(item["id"]) for item in items)

    return JsonResponse(
        {
            "latest": latest,
            "notifications": items,
        }
    )
