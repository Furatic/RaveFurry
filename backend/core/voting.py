"""Durable, atomic votes across queue handoffs and routine web restarts."""
import ast
import datetime
import hashlib
import logging
import re
import uuid

from django.db import transaction
from django.utils import timezone
from redis.exceptions import RedisError

from core import models, queue_lock, redis, user_manager
from core.settings import storage

logger = logging.getLogger(__name__)


class VoteRejected(Exception):
    def __init__(self, message, status=400, state=None):
        super().__init__(message)
        self.status = status
        self.state = state or {}


def voter_key(request):
    ip = user_manager.normalize_ip(user_manager.get_client_ip(request))
    if storage.get("ip_checking") and ip:
        return "ip:" + hashlib.sha256(ip.encode()).hexdigest()
    identity = user_manager.client_identity(request)
    return "browser:" + identity.token_hash


def _active_song(key, occurrence=None):
    current = models.CurrentSong.objects.filter(queue_key=key)
    queued = models.QueuedSong.objects.filter(pk=key)
    if occurrence is not None:
        current = current.filter(occurrence_id=occurrence)
        queued = queued.filter(occurrence_id=occurrence)
    return current.first() or queued.first()


def _engagement_from_redis(key):
    try:
        raw = redis.connection.get(f"engagement-{key}")
        if raw:
            requester, votes = ast.literal_eval(raw)
            colors = {}
            for session in set(votes) | ({requester} if requester else set()):
                color = redis.connection.get(f"color-{session}")
                if color:
                    colors[str(session)] = color
            return {"requester": requester, "votes": votes, "colors": colors}
    except (RedisError, ValueError, TypeError, SyntaxError):
        pass
    return {"requester": None, "votes": {}, "colors": {}}


def _song_state(song):
    key = getattr(song, "queue_key", song.pk)
    state, _ = models.SongVoteState.objects.get_or_create(
        occurrence_id=song.occurrence_id,
        defaults={"engagement": lambda: _engagement_from_redis(key)},
    )
    return state


def _legacy_choice(ip, key):
    try:
        raw = redis.connection.get(str((ip, key))) if ip else None
        choice = int(raw or 0)
        timestamp = redis.connection.get(f"vote-ts:{ip}:{key}") if raw else None
        changed = datetime.datetime.fromtimestamp(float(timestamp), tz=datetime.timezone.utc) if timestamp else None
        return max(-1, min(1, choice)), changed
    except (RedisError, ValueError, TypeError, OverflowError):
        return 0, None


def _response(song, record):
    return {
        "occurrenceId": str(song.occurrence_id),
        "choice": record.choice,
        "voteRevision": record.revision,
        "votes": song.votes,
        "removed": False,
    }


def _remember_engagement(state, request, amount):
    engagement = state.engagement or {"requester": None, "votes": {}, "colors": {}}
    votes = engagement.setdefault("votes", {})
    session = request.session.session_key
    if session:
        choice = max(-1, min(1, int(votes.get(session, 0)) + amount))
        if choice:
            votes[session] = choice
        else:
            votes.pop(session, None)
        try:
            color = user_manager.color_of(session)
            if color:
                engagement.setdefault("colors", {})[session] = color
        except RedisError:
            pass
    state.engagement = engagement
    state.save(update_fields=["engagement"])


def cache_engagement(state, key):
    """Redis is only a presentation cache. Losing it cannot reopen voting."""
    try:
        engagement = state.engagement or {}
        pipe = redis.connection.pipeline()
        pipe.set(f"engagement-{key}", str((engagement.get("requester"), engagement.get("votes", {}))), ex=86400)
        for session, color in engagement.get("colors", {}).items():
            pipe.set(f"color-{session}", color, ex=86400)
        pipe.execute()
    except RedisError:
        logger.warning("could not refresh vote presentation cache")


@transaction.atomic
def seed_requester_vote(request, song):
    """Record the existing automatic starting vote without changing its score."""
    queue_lock.acquire()
    state = _song_state(song)
    models.SongVote.objects.get_or_create(
        occurrence=state, voter_key=voter_key(request),
        defaults={"choice": 1, "changed_at": timezone.now()},
    )
    session = request.session.session_key
    state.engagement["requester"] = session
    state.engagement.setdefault("votes", {})[session] = 1
    try:
        color = user_manager.color_of(session)
        if color:
            state.engagement.setdefault("colors", {})[session] = color
    except RedisError:
        pass
    state.save(update_fields=["engagement"])
    from core import playback_state_backup
    transaction.on_commit(playback_state_backup.snapshot)


@transaction.atomic
def apply_vote(request, key, desired=None, amount=None, occurrence=None, revision=None, mutation_id=""):
    queue_lock.acquire()
    who = voter_key(request)
    song = _active_song(key, occurrence)
    if song is None:
        if occurrence and mutation_id and models.VoteMutation.objects.filter(
            occurrence_id=occurrence, voter_key=who, mutation_id=mutation_id,
        ).exists():
            return {"occurrenceId": str(occurrence), "removed": True}, None, False
        raise VoteRejected("This song has already left the queue. Refreshing its state.", 409)
    if isinstance(song, models.QueuedSong) and song.priority_tier == "extra":
        raise VoteRejected("Voting starts after this song enters the main queue.", 409)
    if storage.get("interactivity") not in (storage.Interactivity.full_voting, storage.Interactivity.upvotes_only):
        raise VoteRejected("Voting is not enabled.", 403)
    identity = user_manager.client_identity(request)
    ip = user_manager.normalize_ip(user_manager.get_client_ip(request))
    owns = (song.requester_token and song.requester_token == identity.token_hash) or (
        not song.requester_token and ip and song.requester_ip == ip
    )
    if owns:
        raise VoteRejected("Your song already starts with one vote; you cannot vote on your own song.", 403)
    state = _song_state(song)
    old, changed = _legacy_choice(ip, key) if storage.get("ip_checking") else (0, None)
    record, _ = models.SongVote.objects.get_or_create(
        occurrence=state, voter_key=who, defaults={"choice": old, "changed_at": changed},
    )
    if mutation_id and models.VoteMutation.objects.filter(
        occurrence=state, voter_key=who, mutation_id=mutation_id,
    ).exists():
        return _response(song, record), None, False
    target = record.choice + amount if desired is None else desired
    if target not in (-1, 0, 1):
        raise VoteRejected("Your vote is already recorded. Refreshing its state.", 409, _response(song, record))
    if target == record.choice:
        return _response(song, record), None, False
    if revision is not None and revision != record.revision:
        raise VoteRejected("Your vote changed in another tab. Its current state has been restored.", 409, _response(song, record))
    if target < 0 and storage.get("interactivity") == storage.Interactivity.upvotes_only:
        raise VoteRejected("Only upvotes are enabled.", 403, _response(song, record))
    now = timezone.now()
    cooldown = float(storage.get("vote_change_cooldown_seconds"))
    if record.changed_at and (now - record.changed_at).total_seconds() < cooldown:
        raise VoteRejected("Please wait before changing this vote again.", 429, _response(song, record))
    if record.choice >= 0 and target < 0:
        cutoff = now - datetime.timedelta(seconds=user_manager.RECENT_VOTE_WINDOW_SECONDS)
        recent = models.SongVote.objects.filter(voter_key=who, activity_recorded=True, changed_at__gte=cutoff)
        downs = recent.filter(choice__lt=0).count()
        ups = recent.filter(choice__gt=0).exclude(pk=record.pk).count()
        if downs >= user_manager.MAX_RECENT_DOWNVOTE_TRANSITIONS and ups < user_manager.MIN_RECENT_UPVOTE_TRANSITIONS:
            raise VoteRejected("You have downvoted too many songs recently.", 429, _response(song, record))
    delta = target - record.choice
    record.choice = target
    record.revision += 1
    record.changed_at = now
    record.activity_recorded = True
    record.save()
    if storage.get("color_indication") != storage.Privileges.nobody:
        _remember_engagement(state, request, delta)
        transaction.on_commit(lambda: cache_engagement(state, key))
    song.votes += delta
    song.save(update_fields=["votes"])
    if mutation_id:
        models.VoteMutation.objects.create(occurrence=state, voter_key=who, mutation_id=mutation_id)
        stale = list(models.VoteMutation.objects.filter(occurrence=state, voter_key=who).order_by("-id").values_list("id", flat=True)[32:])
        if stale:
            models.VoteMutation.objects.filter(id__in=stale).delete()
    removed = None
    should_skip = False
    threshold = -storage.get("downvotes_to_kick")
    if song.votes <= threshold:
        if isinstance(song, models.CurrentSong):
            should_skip = True
        else:
            removed = models.QueuedSong.objects.remove(song.pk, snapshot=False)
    from core import playback_state_backup
    transaction.on_commit(playback_state_backup.snapshot)
    response = _response(song, record)
    response["removed"] = removed is not None
    response["amount"] = delta
    return response, removed, should_skip


def personal_state(request, songs):
    who = voter_key(request)
    choices = {str(v.occurrence_id): v for v in models.SongVote.objects.filter(
        voter_key=who, occurrence_id__in=[s.occurrence_id for s in songs],
    )}
    return [{
        "queueKey": getattr(song, "queue_key", song.pk),
        "occurrenceId": str(song.occurrence_id),
        "choice": choices[str(song.occurrence_id)].choice if str(song.occurrence_id) in choices else 0,
        "voteRevision": choices[str(song.occurrence_id)].revision if str(song.occurrence_id) in choices else 0,
        "eligible": not isinstance(song, models.QueuedSong) or song.priority_tier != "extra",
    } for song in songs]


def retire(occurrence):
    models.SongVoteState.objects.filter(pk=occurrence).update(retired_at=timezone.now())
    models.SongVoteState.objects.filter(retired_at__lt=timezone.now() - datetime.timedelta(hours=24)).delete()


def import_legacy_votes():
    """Run once before startup's Redis flush; preserve any still-available choices."""
    active = list(models.QueuedSong.objects.all()) + list(models.CurrentSong.objects.all())
    songs = {getattr(s, "queue_key", s.pk): s for s in active}
    for song in active:
        _song_state(song)
    try:
        for key in redis.connection.scan_iter(match="('*', *)", count=250):
            try:
                ip, queue_key = ast.literal_eval(key)
                song = songs.get(int(queue_key))
                if not song or not user_manager.normalize_ip(ip):
                    continue
                choice, changed = _legacy_choice(ip, queue_key)
                who = "ip:" + hashlib.sha256(ip.encode()).hexdigest()
                models.SongVote.objects.get_or_create(
                    occurrence_id=song.occurrence_id, voter_key=who,
                    defaults={"choice": choice, "changed_at": changed},
                )
            except (ValueError, TypeError, SyntaxError):
                continue
    except RedisError:
        logger.warning("legacy vote cache was unavailable; existing database scores remain intact")


def restore_presentation_cache():
    active = list(models.QueuedSong.objects.all()) + list(models.CurrentSong.objects.all())
    states = {str(s.pk): s for s in models.SongVoteState.objects.filter(pk__in=[s.occurrence_id for s in active])}
    for song in active:
        state = states.get(str(song.occurrence_id))
        if state:
            cache_engagement(state, getattr(song, "queue_key", song.pk))
