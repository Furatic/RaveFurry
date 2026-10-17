"""Contains the core app configuration."""
import atexit
import logging
import os
import sys

from django.apps import AppConfig

from django.conf import settings as conf
from core.util import strtobool


class CoreConfig(AppConfig):
    """This is the configuration for the core app."""

    name = "core"

    def ready(self) -> None:
        if "celery" in sys.argv and strtobool(os.environ.get("RUN_MAIN", "0")):
            # if the development celery process starts,
            # have it import all modules containing celery tasks
            # this way, its autoreload is notified on changes in these modules

            for module in conf.CELERY_IMPORTS:
                assert isinstance(module, str)
                __import__(module)

            return

        # ready is called for every management command and for autoreload
        # only start raveberry when
        # in debug mode and the main application is run (not autoreload),
        # or when runserver is explicitly launched with --noreload,
        # or in prod mode (run by daphne)
        if "runserver" in sys.argv:
            if "--noreload" in sys.argv:
                start_raveberry = True
            else:
                start_raveberry = strtobool(os.environ.get("RUN_MAIN", "0"))
        else:
            start_raveberry = (
                sys.argv[0].endswith("daphne")
                or strtobool(os.environ.get("FURATIC_LOCAL_HTTP_SERVER", "0"))
            )

        if conf.TESTING:
            from core.musiq import controller

            # when MopidyAPI instances are created too often,
            # mopidy runs into a "Set changed size during iteration" error.
            # Thus we initialize the interface once and not for every testcase
            controller.start()

        if start_raveberry:
            from core import tasks
            from core import redis
            from core.musiq import musiq
            from core.musiq import playback
            from core.settings import basic
            from core.settings import platforms
            from core.lights import worker

            logging.info("starting raveberry")

            from core import runtime_persistence
            runtime_persistence.restore_settings()
            
            from core import playback_state_backup  # pylint: disable=import-outside-toplevel
            playback_state_backup.restore_if_database_empty()

            from core import voting
            voting.import_legacy_votes()
            
            # The runtime cache is reset on a planned recycle, but the next-up
            # reservation must survive the gap between songs.
            from core.musiq import next_up
            locked_queue_key = next_up.get_locked_queue_key()
            redis.start()
            if locked_queue_key is not None:
                next_up.set_locked_queue_key(locked_queue_key)
            voting.restore_presentation_cache()

            from core import site_mode  # pylint: disable=import-outside-toplevel
            site_mode.set_mode(site_mode.get_mode())

            tasks.start()

            worker.start()
            # platforms needs to start before musiq because mopidy_available is checked there
            platforms.start()
            musiq.start()
            basic.start()

            try:
                from django.db.utils import OperationalError, ProgrammingError

                from core import user_manager

                user_manager.ensure_builtin_moderator(rotate_if_unset=True)
            except (OperationalError, ProgrammingError) as exc:
                logging.warning("unable to initialize built-in moderator: %s", exc)

            def stop_workers() -> None:
                from core import obs_export
                obs_export.stop()
                # wake up the playback thread and stop it
                redis.put("stop_playback_loop", True)
                playback.queue_changed.set()

                # wake the buzzer thread so it exits
                playback.buzzer_stopped.set()

                # wake up the listener thread with an instruction to stop the lights worker
                redis.connection.publish("lights_settings_changed", "stop")

            atexit.register(stop_workers)
