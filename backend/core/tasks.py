"""This module contains the celery app."""

import os
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from core.util import strtobool

logger = logging.getLogger(__name__)

app: Any

if strtobool(os.environ.get("DJANGO_NO_CELERY", "0")):
    # For debugging/basic mode, keep async behavior without spawning unlimited
    # threads. Keep substantial headroom for concurrent metadata/download work.
    CELERY_ACTIVE = False
    MOCK_CELERY_WORKERS = int(os.environ.get("RAVEFURRY_MOCK_CELERY_WORKERS", "12"))
    _executor = ThreadPoolExecutor(
        max_workers=max(12, MOCK_CELERY_WORKERS),
        thread_name_prefix="ravefurry-task",
    )

    class MockCelery:
        """A mock class that runs delayed tasks through a bounded thread pool."""

        def task(self, function: Callable) -> Callable:
            """This decorator mocks celery's delay function."""

            def thread_target(*args: Any, **kwargs: Any) -> None:
                from django.db import close_old_connections, connections
                # pylint: disable=import-outside-toplevel

                try:
                    close_old_connections()
                    function(*args, **kwargs)
                except Exception:  # pylint: disable=broad-except
                    # Executor exceptions otherwise disappear into an unused
                    # Future, leaving background failures invisible.
                    logger.exception(
                        "background task %s.%s failed",
                        function.__module__,
                        function.__name__,
                    )
                    raise
                finally:
                    connections.close_all()

            def delay(*args: Any, **kwargs: Any) -> None:
                _executor.submit(thread_target, *args, **kwargs)

            # monkeypatch-add this method
            function.delay = delay  # type: ignore[attr-defined]
            return function

    def start() -> None:
        """MockCelery does not need to be initialized."""

    app = MockCelery()

else:
    CELERY_ACTIVE = True
    from celery import Celery

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "main.settings")
    app = Celery("core")
    app.config_from_object("django.conf:settings")

    class CeleryNotReachable(Exception):
        """Raised when celery should be reachable but is not."""

    def start() -> None:
        """Initializes celery."""
        # check if celery is up and wait for a maximum of 5 seconds
        for _ in range(10):
            if app.control.ping(timeout=0.5):
                break
        else:
            raise CeleryNotReachable(
                "Celery worker pool not reachable. Is it running?"
            )

        # stop running celery tasks from old django instance
        active_tasks = app.control.inspect().active()
        if active_tasks:
            for _, tasks in active_tasks.items():
                for task in tasks:
                    app.control.revoke(task_id=task["id"], terminate=True)

