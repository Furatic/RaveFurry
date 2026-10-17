"""Serialize short queue mutations; keep native playback and file writes outside this lock."""
from django.db import connection


def acquire() -> None:
    from core.models import QueueMutationLock

    if not connection.in_atomic_block:
        raise RuntimeError("Queue mutations must use transaction.atomic")
    QueueMutationLock.objects.get_or_create(pk=1)
    QueueMutationLock.objects.select_for_update().get(pk=1)
