"""This module contains manages the song queue in the database."""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Tuple

from django.db import models, transaction
from django.db.models import F, QuerySet

import core.models
from core import queue_lock

if TYPE_CHECKING:
    from core.models import QueuedSong
    from core.musiq.song_utils import Metadata

def _snapshot_after_commit() -> None:
    try:
        from core import playback_state_backup  # pylint: disable=import-outside-toplevel

        transaction.on_commit(playback_state_backup.snapshot)
    except Exception:  # pylint: disable=broad-except
        pass

class SongQueue(models.Manager):
    """This is the manager for the QueuedSong model.
    Handles all operations on the queue."""

    @transaction.atomic
    def rebalance_priorities(self) -> None:
        """Keep normal songs first and stably round-robin overflow by requester."""
        queue_lock.acquire()
        # Every enqueue may rewrite overflow positions. Lock queue rows in one
        # deterministic order to prevent cross-user submissions deadlocking.
        list(self.select_for_update().order_by("id").values_list("id", flat=True))
        normal = list(self.filter(priority_tier="normal").order_by("index"))
        grouped = {}
        for song in self.filter(priority_tier="extra").order_by("id"):
            grouped.setdefault(song.requester_token or f"song:{song.id}", []).append(song)
        # Preserve the order of each requester's oldest waiting extra. Repeated
        # submissions/state updates must not randomly change the Auto Queue.
        requester_order = list(grouped)
        extra = []
        while requester_order:
            next_round = []
            for requester in requester_order:
                extra.append(grouped[requester].pop(0))
                if grouped[requester]:
                    next_round.append(requester)
            requester_order = next_round
        for index, song in enumerate(normal + extra, start=1):
            if song.index != index:
                self.filter(id=song.id).update(index=index)
        _snapshot_after_commit()

    @transaction.atomic
    def promote_oldest_extra(self, requester_token: str) -> None:
        """Promote this requester's oldest overflow song, if one exists."""
        queue_lock.acquire()
        from core import site_mode
        if site_mode.is_closing():
            return
        if not requester_token or self.filter(
            requester_token=requester_token,
            priority_tier="normal",
        ).exists() or core.models.CurrentSong.objects.filter(
            requester_token=requester_token,
        ).exists():
            return
        overflow = self.filter(
            requester_token=requester_token,
            priority_tier="extra",
        ).order_by("id").first()
        if overflow:
            overflow.priority_tier = "normal"
            # Join behind the existing main queue even if a moderator previously
            # reordered the Auto Queue. Voting/next-up ordering remains intact.
            overflow.index = (
                self.aggregate(last_index=models.Max("index"))["last_index"] or 0
            ) + 1
            overflow.save(update_fields=["priority_tier", "index"])
            self.rebalance_priorities()

    @transaction.atomic
    def confirmed(self) -> QuerySet[QueuedSong]:
        """Returns a QuerySet containing all confirmed songs.
        Confirmed songs are not in the process of being made available."""
        return self.exclude(internal_url=None)

    def playable(self) -> QuerySet[QueuedSong]:
        """Auto Queue songs must be promoted before any playback path selects them."""
        return self.confirmed().filter(
            priority_tier="normal", review_status__in=["clear", "approved"]
        )

    @transaction.atomic
    def delete_placeholders(self) -> None:
        """Deletes all songs from the queue that are not confirmed."""
        queue_lock.acquire()
        from core import voting
        affected_requesters = list(
            self.filter(internal_url=None, priority_tier="normal")
            .exclude(requester_token="")
            .order_by()
            .values_list("requester_token", flat=True)
            .distinct()
        )
        for occurrence in self.filter(internal_url=None).values_list("occurrence_id", flat=True):
            voting.retire(occurrence)
        self.filter(internal_url=None).delete()
        for requester in affected_requesters:
            self.promote_oldest_extra(requester)
        _snapshot_after_commit()

    @transaction.atomic
    def enqueue(
        self,
        metadata: "Metadata",
        manually_requested: bool,
        votes=0,
        enqueue_first=False,
        requester_ip: str = "",
        requester_session_key: str = "",
    ) -> QueuedSong:
        """Creates a new song at the end of the queue and returns it."""
        queue_lock.acquire()
        from core import site_mode
        if not site_mode.accepts_song_requests():
            from core.musiq.music_provider import ProviderError
            raise ProviderError(
                "Closing time: we are no longer accepting new songs. Thanks for coming!"
                if site_mode.is_closing() else "FURATIC is currently in After Hours mode."
            )
        last = self.last()
        index = 1 if last is None else last.index + 1
        song = self.create(
            index=index,
            votes=votes,
            manually_requested=manually_requested,
            artist=metadata["artist"],
            title=metadata["title"],
            duration=metadata["duration"],
            internal_url=metadata["internal_url"],
            external_url=metadata["external_url"],
            stream_url=metadata["stream_url"],
            requester_ip=requester_ip,
            requester_session_key=requester_session_key,
        )
        if enqueue_first:
            self.prioritize(song.id)
        _snapshot_after_commit()
        return song

    @transaction.atomic
    def dequeue(self) -> Tuple[int, Optional["QueuedSong"]]:
        """Removes the first completed song from the queue and returns its id and the object."""
        queue_lock.acquire()
        from core import user_manager
        from core.musiq import next_up

        eligible = self.playable()
        locked_key = next_up.get_locked_queue_key()
        if locked_key is not None:
            song = eligible.filter(id=locked_key).first()
            if song is None:
                next_up.clear_locked_queue_key()
                song = eligible.first()
        else:
            song = eligible.first()

        if song is None:
            return -1, None

        song_id = song.id
        user_manager.release_queue_slot_for_song(song_id)
        next_up.clear_if_locked(song_id)
        song.delete()
        self.filter(index__gt=song.index).update(index=F("index") - 1)
        self.rebalance_priorities()
        return song_id, song

    @transaction.atomic
    def prioritize(self, key: int) -> None:
        """Moves the song specified by :param key: to the front of the queue."""
        queue_lock.acquire()
        from core.musiq import next_up

        locked_key = next_up.get_locked_queue_key()
        if locked_key is not None and int(key) != locked_key:
            raise ValueError("next up song is locked")

        to_prioritize = self.get(id=key)
        first = self.first()
        if to_prioritize == first:
            return

        self.filter(index__lt=to_prioritize.index).update(index=F("index") + 1)
        to_prioritize.index = 1
        to_prioritize.save()
        _snapshot_after_commit()

    @transaction.atomic
    def deprioritize(self, key: int) -> None:
        """Moves the song specified by :param key: to the end of the queue."""
        queue_lock.acquire()
        to_deprioritize = self.get(id=key)
        last = self.last()
        if to_deprioritize == last:
            return

        self.filter(index__gt=to_deprioritize.index).update(index=F("index") - 1)
        to_deprioritize.index = self.count()
        to_deprioritize.save()
        _snapshot_after_commit()

    @transaction.atomic
    def remove(self, key: int, snapshot: bool = True, transfer: bool = False) -> "QueuedSong":
        """Removes the song specified by :param key: from the queue and returns it."""
        queue_lock.acquire()
        from core import user_manager
        from core.musiq import next_up

        to_remove = self.get(id=key)
        requester_token = to_remove.requester_token
        was_normal = to_remove.priority_tier == "normal"
        user_manager.release_queue_slot_for_song(key)
        next_up.clear_if_locked(key)
        to_remove.delete()
        self.filter(index__gt=to_remove.index).update(index=F("index") - 1)
        if was_normal and not transfer:
            self.promote_oldest_extra(requester_token)
        if not transfer:
            from core import voting
            voting.retire(to_remove.occurrence_id)
        if snapshot:
            _snapshot_after_commit()
        return to_remove

    @transaction.atomic
    def reorder(
        self, new_prev_id: Optional[int], element_id: int, new_next_id: Optional[int]
    ) -> None:
        """Moves the song specified by :param element_id:
        between the two songs :param new_prev_id: and :param new_next_id:."""
        queue_lock.acquire()

        new_prev = self.filter(id=new_prev_id).first()
        try:
            to_reorder = self.get(id=element_id)
        except core.models.QueuedSong.DoesNotExist as error:
            raise ValueError("reordered song does not exist") from error
        new_next = self.filter(id=new_next_id).first()

        from core.musiq import next_up

        locked_key = next_up.get_locked_queue_key()
        if locked_key is not None:
            if element_id == locked_key:
                raise ValueError("next up song is locked")
            if new_next_id == locked_key:
                raise ValueError("cannot move a song ahead of locked next up song")

        first = self.first()
        last = self.last()
        # check validity of request
        if new_prev is None and new_next is None:
            # to_reorder has to be the only element in the queue
            if first != to_reorder or last != to_reorder:
                raise ValueError("reordered song is not the only one")
            # nothing to do
            return
        if new_prev is None:
            # new_next has to be the first element
            if new_next != first:
                raise ValueError("given first is not head of the queue")
            assert new_next
            self.prioritize(element_id)
            return
        if new_next is None:
            # new_prev has to be the last element
            if new_prev != last:
                raise ValueError("given last is not tail of the queue")
            self.deprioritize(element_id)
            return
        # neither new_prev and new_next are None
        # new_prev and new_next have to be adjacent
        if new_next.index != new_prev.index + 1:
            raise ValueError("given pair of songs is not adjacent")

        # update indices
        moving_up = True
        if new_prev.index > to_reorder.index or new_next.index > to_reorder.index:
            moving_up = False

        to_update = self.all()
        if moving_up:
            new_index = new_next.index
            to_update = to_update.filter(index__lte=to_reorder.index - 1)
            to_update = to_update.filter(index__gte=new_next.index)
            to_update.update(index=F("index") + 1)
        else:
            new_index = new_prev.index
            to_update = to_update.filter(index__lte=new_prev.index)
            to_update = to_update.filter(index__gte=to_reorder.index + 1)
            to_update.update(index=F("index") - 1)

        to_reorder.index = new_index
        to_reorder.save()
        _snapshot_after_commit()

    @transaction.atomic
    def vote(self, key: int, amount: int, threshold: int) -> Optional["QueuedSong"]:
        """Modify the vote-count of the song specified by :param key: by :param amount: votes.
        If the song is now below the threshold, remove and return it."""
        queue_lock.acquire()
        from core import user_manager
        from core.musiq import next_up

        self.filter(id=key).update(votes=F("votes") + amount)
        _snapshot_after_commit()
        try:
            song = self.get(id=key)
            if song.votes <= threshold:
                return self.remove(key)
        except core.models.QueuedSong.DoesNotExist:
            pass
        return None
