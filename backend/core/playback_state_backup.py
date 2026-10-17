"""DB-independent backup of live playback state.

This mirrors CurrentSong and QueuedSong to a small JSON file so a local
PostgreSQL restart/recreate does not wipe the active event queue.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import uuid
from typing import Any, Dict, Optional

from django.conf import settings as conf
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core import models, redis, queue_lock
from core.settings import storage

logger = logging.getLogger(__name__)
_snapshot_lock = threading.RLock()

STATE_FILE = pathlib.Path(conf.BASE_DIR) / "config" / "playback_state_backup.json"

QUEUE_FIELDS = (
    "occurrence_id",
    "index",
    "manually_requested",
    "votes",
    "internal_url",
    "external_url",
    "stream_url",
    "artist",
    "title",
    "duration",
    "requester_ip",
    "requester_session_key",
    "requester_token", "priority_tier", "review_status", "review_reason",
    "lyrics", "profanity_count", "slur_count", "artwork_url", "genre",
)

CURRENT_FIELDS = (
    "occurrence_id", "playback_outcome",
    "queue_key",
    "manually_requested",
    "votes",
    "internal_url",
    "external_url",
    "stream_url",
    "artist",
    "title",
    "duration",
    "requester_ip",
    "requester_session_key",
    "requester_token", "artwork_url", "genre",
)


def _datetime_to_string(value) -> str:
    if value is None:
        return timezone.now().isoformat()
    return value.isoformat()


def _string_to_datetime(value: Any):
    parsed = parse_datetime(str(value or ""))
    if parsed is None:
        return timezone.now()
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _song_payload(song, fields) -> Dict[str, Any]:
    payload = {}
    for field in fields:
        value = getattr(song, field)
        payload[field] = str(value) if isinstance(value, uuid.UUID) else value
    return payload


def _current_payload(song: models.CurrentSong) -> Dict[str, Any]:
    payload = _song_payload(song, CURRENT_FIELDS)
    payload["created"] = _datetime_to_string(song.created)
    payload["last_paused"] = _datetime_to_string(song.last_paused)
    payload["playback_started_at"] = song.playback_started_at.isoformat() if song.playback_started_at else None
    return payload


def _atomic_write(payload: Dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(".tmp")

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary_file, STATE_FILE)


def snapshot() -> None:
    """Mirror a consistent queue and its vote ledger without holding DB locks during fsync."""
    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(snapshot)
        return
    try:
        # Serialize captures/writes so an older snapshot cannot replace a newer one.
        with _snapshot_lock:
            with transaction.atomic():
                queue_lock.acquire()
                current_song = models.CurrentSong.objects.first()
                queued_songs = list(models.QueuedSong.objects.order_by("index", "id"))
                active = [s.occurrence_id for s in queued_songs]
                if current_song:
                    active.append(current_song.occurrence_id)
                payload = {
                    "version": 2,
                    "saved_at": timezone.now().isoformat(),
                    "paused": bool(redis.get("paused")),
                    "current": _current_payload(current_song) if current_song else None,
                    "queue": [_song_payload(song, QUEUE_FIELDS) for song in queued_songs],
                    "vote_states": [{"occurrence": str(v.pk), "engagement": v.engagement}
                                    for v in models.SongVoteState.objects.filter(pk__in=active)],
                    "votes": [{"occurrence": str(v.occurrence_id), "voter_key": v.voter_key,
                               "choice": v.choice, "revision": v.revision,
                               "changed_at": v.changed_at.isoformat() if v.changed_at else None,
                               "activity_recorded": v.activity_recorded}
                              for v in models.SongVote.objects.filter(occurrence_id__in=active)],
                    "vote_mutations": [{"occurrence": str(v.occurrence_id), "voter_key": v.voter_key,
                                         "mutation_id": v.mutation_id}
                                        for v in models.VoteMutation.objects.filter(occurrence_id__in=active)],
                }
            _atomic_write(payload)
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to snapshot playback state: %s", error)


def snapshot_on_commit() -> None:
    """Snapshot after the active DB transaction commits."""

    try:
        transaction.on_commit(snapshot)
    except Exception:  # pylint: disable=broad-except
        snapshot()


def _load_payload() -> Optional[Dict[str, Any]]:
    if not STATE_FILE.exists():
        return None

    try:
        with open(STATE_FILE, encoding="utf-8") as file:
            payload = json.load(file)

        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", 0)) not in (1, 2):
            return None

        return payload
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to read playback state backup: %s", error)
        return None


def _occurrence(item):
    return uuid.UUID(str(item["occurrence_id"])) if item.get("occurrence_id") else uuid.uuid4()


def _create_queued_song(item: Dict[str, Any]) -> None:
    models.QueuedSong.objects.create(
        occurrence_id=_occurrence(item),
        index=int(item.get("index") or 1),
        manually_requested=bool(item.get("manually_requested")),
        votes=int(item.get("votes") or 0),
        internal_url=item.get("internal_url"),
        external_url=str(item.get("external_url") or ""),
        stream_url=item.get("stream_url"),
        artist=str(item.get("artist") or ""),
        title=str(item.get("title") or ""),
        duration=float(item.get("duration") or -1),
        requester_ip=str(item.get("requester_ip") or ""),
        requester_session_key=str(item.get("requester_session_key") or ""),
        requester_token=str(item.get("requester_token") or ""),
        priority_tier=str(item.get("priority_tier") or "normal"),
        review_status=str(item.get("review_status") or "clear"),
        review_reason=str(item.get("review_reason") or ""),
        lyrics=str(item.get("lyrics") or ""),
        profanity_count=int(item.get("profanity_count") or 0),
        slur_count=int(item.get("slur_count") or 0),
        artwork_url=str(item.get("artwork_url") or ""),
        genre=str(item.get("genre") or ""),
    )


def _create_current_song(item: Dict[str, Any]) -> None:
    current_song = models.CurrentSong.objects.create(
        occurrence_id=_occurrence(item),
        playback_outcome=str(item.get("playback_outcome") or "completed"),
        playback_started_at=_string_to_datetime(item["playback_started_at"]) if item.get("playback_started_at") else None,
        queue_key=int(item.get("queue_key") or -1),
        manually_requested=bool(item.get("manually_requested")),
        votes=int(item.get("votes") or 0),
        internal_url=str(item.get("internal_url") or ""),
        external_url=str(item.get("external_url") or ""),
        stream_url=item.get("stream_url"),
        artist=str(item.get("artist") or ""),
        title=str(item.get("title") or ""),
        duration=float(item.get("duration") or -1),
        requester_ip=str(item.get("requester_ip") or ""),
        requester_session_key=str(item.get("requester_session_key") or ""),
        requester_token=str(item.get("requester_token") or ""),
        artwork_url=str(item.get("artwork_url") or ""),
        genre=str(item.get("genre") or ""),
    )

    # CurrentSong uses auto_now_add, so update timestamps after creation.
    models.CurrentSong.objects.filter(pk=current_song.pk).update(
        created=_string_to_datetime(item.get("created")),
        last_paused=_string_to_datetime(item.get("last_paused")),
    )


def restore_if_database_empty() -> bool:
    """Restore playback state only when DB playback tables are empty."""

    payload = _load_payload()
    if not payload:
        return False

    try:
        if models.CurrentSong.objects.exists() or models.QueuedSong.objects.exists():
            return False

        queue_items = payload.get("queue") or []
        current_item = payload.get("current")

        if not queue_items and not current_item:
            return False

        with transaction.atomic():
            queue_lock.acquire()
            if models.CurrentSong.objects.exists() or models.QueuedSong.objects.exists():
                return False
            for item in sorted(
                queue_items,
                key=lambda queue_item: int(queue_item.get("index") or 0),
            ):
                _create_queued_song(item)

            if current_item:
                _create_current_song(current_item)

            active = set(models.QueuedSong.objects.values_list("occurrence_id", flat=True))
            active.update(models.CurrentSong.objects.values_list("occurrence_id", flat=True))
            for entry in payload.get("vote_states", []):
                occurrence = uuid.UUID(entry["occurrence"])
                if occurrence in active:
                    models.SongVoteState.objects.update_or_create(pk=occurrence, defaults={"engagement": entry.get("engagement", {}), "retired_at": None})
            for entry in payload.get("votes", []):
                occurrence = uuid.UUID(entry["occurrence"])
                if occurrence in active and entry.get("choice") in (-1, 0, 1):
                    state, _ = models.SongVoteState.objects.get_or_create(pk=occurrence)
                    models.SongVote.objects.update_or_create(occurrence=state, voter_key=entry["voter_key"], defaults={
                        "choice": entry["choice"], "revision": int(entry.get("revision", 0)),
                        "changed_at": _string_to_datetime(entry["changed_at"]) if entry.get("changed_at") else None,
                        "activity_recorded": bool(entry.get("activity_recorded")),
                    })
            for entry in payload.get("vote_mutations", []):
                occurrence = uuid.UUID(entry["occurrence"])
                if occurrence in active:
                    state, _ = models.SongVoteState.objects.get_or_create(pk=occurrence)
                    models.VoteMutation.objects.get_or_create(occurrence=state, voter_key=entry["voter_key"], mutation_id=entry["mutation_id"])

        if "paused" in payload:
            storage.put("paused", bool(payload.get("paused")))

        logger.info(
            "restored playback state from backup: current=%s queued=%s",
            bool(current_item),
            len(queue_items),
        )
        return True
    except Exception as error:  # pylint: disable=broad-except
        logger.warning("failed to restore playback state backup: %s", error)
        return False
