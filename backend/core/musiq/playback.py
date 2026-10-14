"""This module handles playback flow of music."""

from __future__ import annotations

import datetime
import logging
import os
import random
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

from django.conf import settings as conf
from django.db import connection, transaction
from django.db.models import Case, F, IntegerField, Value, When
from django.utils import timezone

from core import models, obs_export, redis, user_manager, playback_state_backup, site_mode
from core.lights import controller as lights_controller
from core.musiq import audio_tail, musiq, next_up, player
from core.settings import storage
from core.tasks import app
from core.musiq import song_utils
from core.models import CurrentSong
from core.safe_files import retry_file_operation, safe_unlink

queue_changed = redis.Event("queue_changed")
buzzer_stopped = redis.Event("buzzer_stopped")

queue = models.QueuedSong.objects

def _ordered_confirmed_queue():
    confirmed = queue.playable().annotate(queue_rank=Case(When(priority_tier="extra", then=Value(1)), default=Value(0), output_field=IntegerField()))
    voting_enabled = storage.get("interactivity") in [
        storage.Interactivity.upvotes_only,
        storage.Interactivity.full_voting,
    ]

    if voting_enabled:
        natural_order = confirmed.annotate(
            effective_votes=Case(
                When(votes__gte=1, then=F("votes")),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("queue_rank", "-effective_votes", "index")
    else:
        natural_order = confirmed.order_by("queue_rank", "index")

    locked_key = next_up.resolve_locked_queue_key(
        natural_order.values_list("id", flat=True),
        allow_new_lock=False,
    )

    if voting_enabled:
        ordered = confirmed.annotate(
            effective_votes=Case(
                When(votes__gte=1, then=F("votes")),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        if locked_key is not None:
            ordered = ordered.annotate(
                next_up_locked=Case(
                    When(id=locked_key, then=Value(0)),
                    default=Value(1),
                    output_field=IntegerField(),
                )
            ).order_by("next_up_locked", "queue_rank", "-effective_votes", "index")
        else:
            ordered = ordered.order_by("queue_rank", "-effective_votes", "index")
        return ordered

    if locked_key is not None:
        return confirmed.annotate(
            next_up_locked=Case(
                When(id=locked_key, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by("next_up_locked", "queue_rank", "index")

    return confirmed.order_by("queue_rank", "index")

def request_operator_command(command: str) -> None:
    """Send an operator command from the launcher process to the playback loop."""
    redis.put("operator_command", command)
    queue_changed.set()

class PlaybackError(Exception):
    pass


def start() -> None:
    """Initializes this module by starting the playback and buzzer loop."""
    paused = storage.get("paused")
    redis.put("paused", paused)
    redis.put("volume", storage.get("volume"))

    # scrub stale runtime flags from previous runs
    redis.put("playing", False)
    redis.put("backup_playing", False)
    redis.put("operator_command", "")
    redis.put("stop_playback_loop", False)
    redis.put("playback_worker_failed", False)

    _handle_buzzer.delay()
    _loop.delay()


class Playback:
    """Class containing all playback related methods."""

    def __init__(self):
        from core.musiq.fake_player import FakePlayer

        redis.put("playing", False)

        queue.delete_placeholders()

        self.players = {
            "fake": FakePlayer(),
        }

        if os.name == "nt":
            from core.musiq.windows_player import WindowsPlayer
            self.players["windows"] = WindowsPlayer()

        if redis.get("spotify_available"):
            from core.musiq.spotify_player import SpotifyPlayer
            self.players["spotify"] = SpotifyPlayer()

        if redis.get("mopidy_available"):
            from core.musiq.mopidy_player import MopidyPlayer
            self.players["mopidy"] = MopidyPlayer()

        output = storage.get("output")

        if os.name == "nt" and output == "":
            output = "windows-default"
            storage.put("output", output)

        if redis.get("spotify_available") and output.startswith("spotify-"):
            redis.put("active_player", "spotify")
        elif os.name == "nt" and output == "windows-default":
            redis.put("active_player", "windows")
        elif not redis.get("mopidy_available"):
            redis.put("active_player", "fake")
        else:
            redis.put("active_player", "mopidy")

    def player(self):
        return self.players[redis.get("active_player")]

    def _handle_operator_command(self) -> str:
        """Handle operator commands sent from the launcher process."""
        command = str(redis.get("operator_command"))
        if not command:
            return ""

        redis.put("operator_command", "")

        if command == "pause_for_afterhours":
            if models.CurrentSong.objects.exists() and not redis.get("paused"):
                musiq.controller._pause()
                musiq.update_state()
            else:
                storage.put("paused", True)
                redis.put("paused", True)
            return command

        if command == "resume_from_afterhours":
            if site_mode.is_afterhours():
                return command  # Closing may already have drained before this command arrived.
            if models.CurrentSong.objects.exists() and redis.get("paused"):
                musiq.controller._resume()
                musiq.update_state()
            else:
                storage.put("paused", False)
                redis.put("paused", False)
                queue_changed.set()
            return command

        if command == "clear_for_event":
            try:
                self.player().skip()
            except Exception:  # pylint: disable=broad-except
                pass

            from core import queue_lock, played_history, voting
            with transaction.atomic():
                queue_lock.acquire()
                for current in models.CurrentSong.objects.all():
                    played_history.record_finished(current, outcome="cleared")
                    voting.retire(current.occurrence_id)
                for occurrence in queue.values_list("occurrence_id", flat=True):
                    voting.retire(occurrence)
                models.CurrentSong.objects.all().delete()
                user_manager.clear_queue_slots()
                queue.all().delete()

            storage.put("paused", False)
            redis.put("paused", False)
            redis.put("playing", False)
            redis.put("backup_playing", False)

            playback_state_backup.snapshot()
            musiq.update_state()
            return command

        return ""

    def _finish_closing_if_drained(self) -> bool:
        """Enter after-hours only after the last main-queue occurrence is gone."""
        if not site_mode.is_closing():
            return False
        from core import audit_log, queue_lock
        with transaction.atomic():
            queue_lock.acquire()
            # Recheck under the same lock used by submissions and moderator mode changes.
            if (not site_mode.is_closing() or models.CurrentSong.objects.exists()
                    or queue.filter(priority_tier="normal").exists()):
                return False
            storage.put("paused", True)
            redis.put("paused", True)
            redis.put("playing", False)
            redis.put("backup_playing", False)
            site_mode.set_mode(site_mode.AFTER_HOURS_MODE)
        audit_log.append("closing_queue_finished", target=site_mode.AFTER_HOURS_MODE)
        musiq.update_state()
        playback_state_backup.snapshot()
        return True
    
    def play_alarm(self, interrupt=False, from_buzzer=True) -> None:
        """Play the alarm sound. If specified, interrupts the currently playing song."""
        redis.put("alarm_playing", True)
        lights_controller.alarm_started()

        success_probability = storage.get("buzzer_success_probability")
        if success_probability >= 0 and from_buzzer:
            if random.random() <= success_probability:
                folder = "resources/sounds/yes"
            else:
                folder = "resources/sounds/no"
            choice = random.choice(os.listdir(os.path.join(conf.BASE_DIR, folder)))
            path = os.path.join(conf.BASE_DIR, folder, choice)
        else:
            path = os.path.join(conf.BASE_DIR, "resources/sounds/alarm.m4a")
        duration = song_utils.get_metadata(path)["duration"]

        redis.put("alarm_duration", duration)

        self.player().play_alarm(interrupt, path)

        musiq.update_state()

        # TODO: is it a very bad idea to simply sleep here for the duration of the alarm?
        # self._wait_until_song_end()
        time.sleep(duration)

        lights_controller.alarm_stopped()
        redis.put("alarm_playing", False)

        if not interrupt:
            # if no song immediately continues playing, a manual state update is needed
            musiq.update_state()

    def _get_next_song(self) -> Tuple[Optional[models.CurrentSong], bool]:
        """Returns the next song that should be played, or None if no song should be played.
        Additionally returns whether the song was recovered from the database (True)
        or dequeued from the queue (False)."""
        # TODO: is this playback_started clear necessary?
        # self.playback_started.clear()

        if models.CurrentSong.objects.exists():
            # recover interrupted song from database
            return models.CurrentSong.objects.get(), True

        self._finish_closing_if_drained()

        if queue.count() == 0:
            queue_changed.wait()
            queue_changed.clear()

            # restart the loop to check again whether songs are available
            # in case of a false wakeup this causes as to wait again
            return None, False

        # Handoff and voting share a short DB lock. No promotion or file/native
        # work runs while a primary song is moving from queued to current.
        from core import queue_lock
        with transaction.atomic():
            queue_lock.acquire()
            existing = models.CurrentSong.objects.first()
            if existing is not None:
                return existing, True
            song = None
            song_id = -1
            if storage.get("interactivity") in [
                storage.Interactivity.upvotes_only,
                storage.Interactivity.full_voting,
            ]:
                song = _ordered_confirmed_queue().first()
                if song is not None:
                    song_id = song.id
                    song = queue.remove(song_id, snapshot=False, transfer=True)
            else:
                song_id, song = queue.dequeue()
            if song is not None and song.internal_url != "alarm":
                current_song = models.CurrentSong.objects.create(
                    queue_key=song_id,
                    occurrence_id=song.occurrence_id,
                    manually_requested=song.manually_requested,
                    votes=song.votes,
                    internal_url=song.internal_url,
                    external_url=song.external_url,
                    stream_url=song.stream_url,
                    artist=song.artist,
                    title=song.title,
                    duration=song.duration,
                    requester_ip=song.requester_ip,
                    requester_session_key=song.requester_session_key,
                    requester_token=song.requester_token,
                    artwork_url=song.artwork_url,
                    genre=song.genre,
                )
        if song is None:
            queue_changed.wait()
            queue_changed.clear()
            return None, False
        if song.internal_url == "alarm":
            self.play_alarm()
            return None, False
        redis.put("backup_playing", False)
        assert song.internal_url
        cover_source = (
            Path(conf.FURATIC_OBS_OUTPUT_DIR).expanduser()
            / "artwork"
            / f"queue-{song_id}.jpg"
        )
        cover_target = (
            Path(conf.FURATIC_OBS_OUTPUT_DIR).expanduser()
            / "songcover.jpg"
        )
        
        if cover_source.is_file():
            try:
                cover_temporary = cover_target.with_suffix(".tmp")
                shutil.copyfile(cover_source, cover_temporary)
        
                retry_file_operation(
                    lambda: os.replace(cover_temporary, cover_target),
                    description="replace OBS song cover",
                    logger=logging.getLogger(__name__),
                )
            except OSError:
                logging.exception("failed to export OBS song cover")
        else:
            safe_unlink(
                cover_target,
                missing_ok=True,
                logger=logging.getLogger(__name__),
            )

        playback_state_backup.snapshot()
        handle_autoplay()

        try:
            archived_song = models.ArchivedSong.objects.get(
                url=current_song.external_url
            )
            votes: Optional[int]
            if storage.get("interactivity") in [
                storage.Interactivity.upvotes_only,
                storage.Interactivity.full_voting,
            ]:
                votes = current_song.votes
            else:
                votes = None
            if storage.get("logging_enabled"):
                models.PlayLog.objects.create(
                    song=archived_song,
                    manually_requested=current_song.manually_requested,
                    votes=votes,
                )
        except (
            models.ArchivedSong.DoesNotExist,
            models.ArchivedSong.MultipleObjectsReturned,
        ):
            pass

        return current_song, False

    @staticmethod
    def _catch_up(current_song: models.CurrentSong, recovered: bool) -> Optional[int]:
        catch_up = None

        if recovered:
            # continue with the current song (approximately) where we last left
            if redis.get("paused"):
                catch_up = round(
                    (current_song.last_paused - current_song.created).total_seconds()
                    * 1000
                )
            else:
                catch_up = round(
                    (timezone.now() - current_song.created).total_seconds() * 1000
                )
            if catch_up > current_song.duration * 1000:
                catch_up = -1

        return catch_up

    def _fade_out_for_transition(self, seconds: float) -> None:
        """Fade out the active player before advancing to the next song.

        Do not call skip()/stop() here. The normal playback loop will delete the
        current song and start the next one. Hard-stopping VLC during this handoff
        can leave the next media item visually active in the UI but silent.
        """
        seconds = max(0.0, float(seconds or 0.0))
        if seconds <= 0:
            return

        player_obj = self.player()
        if not hasattr(player_obj, "set_volume"):
            return

        try:
            restore_volume = float(storage.get("volume"))
        except Exception:  # pylint: disable=broad-except
            restore_volume = 1.0

        restore_volume = max(0.0, min(1.0, restore_volume))
        steps = max(8, int(seconds / 0.1))
        sleep_seconds = seconds / steps

        try:
            for step in range(steps, -1, -1):
                if redis.get("paused") or redis.get("stop_playback_loop") or redis.get("alarm_requested"):
                    break

                volume = restore_volume * (step / steps)
                player_obj.set_volume(volume)
                time.sleep(sleep_seconds)
        finally:
            try:
                player_obj.set_volume(restore_volume)
            except Exception:  # pylint: disable=broad-except
                pass

    def _wait_until_song_end(self) -> bool:
        """Wait until the song is over.
        Returns True when finished without errors, False otherwise."""
        # This is the event based approach. Unfortunately too error-prone.
        # If mopidy crashes/restarts for example, no track_playback_ended event is sent
        # playback_ended.wait()
        error = False

        try:
            starting_song = CurrentSong.objects.get()
        except CurrentSong.DoesNotExist:
            return False

        from core import played_history
        if not redis.get("paused") and self.player().has_started_playback():
            played_history.mark_started(starting_song)

        effective_duration = audio_tail.effective_duration_for_song(starting_song)
        original_duration = float(starting_song.duration or 0.0)
        trim_tail = effective_duration < (original_duration - 0.25)
        transition_fade_seconds = audio_tail.transition_fade_seconds() if trim_tail else 0.0
        fade_started = False
        last_obs_export_at = 0.0

        if trim_tail:
            logging.info(
                "quiet tail trim active for %s: original=%.2fs effective=%.2fs saved=%.2fs",
                starting_song.displayname(),
                original_duration,
                effective_duration,
                original_duration - effective_duration,
            )

        while True:
            command = self._handle_operator_command()
            if command == "clear_for_event":
                return False

            current_song = CurrentSong.objects.get()
            paused = redis.get("paused")
            stop_waiting = False
            try:
                stop_waiting = self.player().should_stop_waiting(error)
            except PlaybackError:
                error = True
            if starting_song.playback_started_at is None and not paused and self.player().has_started_playback():
                played_history.mark_started(starting_song)
            if stop_waiting:
                break
            if paused:
                # stay in the loop, the song won't end while paused
                # progress = (current_song.last_paused - current_song.created).total_seconds()
                pass
            else:
                progress = (timezone.now() - current_song.created).total_seconds()
                progress_for_display = max(0.0, min(effective_duration, progress))

                now_monotonic = time.monotonic()
                if now_monotonic - last_obs_export_at >= 1.0:
                    last_obs_export_at = now_monotonic
                    obs_export.write_current_song_tick(
                        current_song,
                        progress_for_display,
                        effective_duration,
                    )

                if trim_tail and not fade_started:
                    fade_start = max(0.0, effective_duration - transition_fade_seconds)
                    if progress >= fade_start:
                        fade_started = True
                        self._fade_out_for_transition(transition_fade_seconds)
                        break

                if progress >= current_song.duration:
                    break
            # use internal timekeeping to decide when a song is over to lower mopidy performance use
            # current_song = models.CurrentSong.objects.get()
            # if (timezone.now() - current_song.created).total_seconds() > current_song.duration:
            #    break
            time.sleep(0.1)
            if redis.get("stop_playback_loop"):
                # in order to stop the playback thread, return False, making the main loop restart.
                # it will check this variable again and terminate itself.
                return False
            if redis.get("alarm_requested"):
                redis.put("alarm_requested", False)
                self.play_alarm(interrupt=True)
                # the current song was interrupted and needs to be resumed at the correct position
                # returning False will notify the main loop about this interruption,
                # making it restart the song correctly
                # we don't want the song to skip over the time when the alarm was playing
                # thus, we offset the creation date of the current song by the length of the alarm
                # Warning: if this duration does not fit the duration of the actual alarm,
                # Raveberry's internal state gets desynced and weird errors happen
                current_song.created += datetime.timedelta(
                    seconds=redis.get("alarm_duration")
                )
                current_song.save(update_fields=["created"])
                playback_state_backup.snapshot()

                return False
        return not error

    def _song_finished(self, current_song: models.CurrentSong) -> None:
        """Handles things that might need to happen after a song ended,
        e.g.repeat, alarm and backup stream."""
        models.RecentPlay.objects.create(song_url=current_song.external_url)
        models.RecentPlay.objects.filter(
            created__lt=timezone.now() - datetime.timedelta(hours=24),
        ).delete()
        if self._finish_closing_if_drained():
            return
        if storage.get("repeat") and site_mode.accepts_song_requests():
            from core.musiq.music_provider import ProviderError
            try:
                queue.enqueue(
                    {
                        "artist": current_song.artist,
                        "title": current_song.title,
                        "duration": current_song.duration,
                        "internal_url": current_song.internal_url,
                        "external_url": current_song.external_url,
                        "stream_url": current_song.stream_url,
                    },
                    False,
                    enqueue_first=False,
                )
            except ProviderError:
                if site_mode.accepts_song_requests():
                    raise
                if self._finish_closing_if_drained():
                    return
            else:
                queue_changed.set()

        if user_manager.partymode_enabled() and random.random() < storage.get(
            "alarm_probability"
        ):
            self.play_alarm(from_buzzer=False)

        if not queue.exists() and storage.get("backup_stream"):
            redis.put("backup_playing", True)
            self.player().play_backup_stream()

        musiq.update_state()

    def loop(self) -> None:
        """The main loop of the player.
        Takes a song from the queue and plays it until it is finished."""

        while True:
            from core import runtime_restart  # pylint: disable=import-outside-toplevel

            if runtime_restart.restart_requested():
                time.sleep(0.25)
                continue

            command = self._handle_operator_command()
            if command == "clear_for_event":
                continue

            if redis.get("playback_error"):
                # When the backend comes back after a transient error, probe it and
                # clear the runtime error flag so skip/next can recover automatically.
                try:
                    self.player().should_stop_waiting(True)
                except PlaybackError:
                    time.sleep(0.5)
                    continue

                player.set_playback_error(False)
                time.sleep(0.1)
                continue

            if redis.get("stop_playback_loop"):
                break

            current_song, recovered = self._get_next_song()
            if current_song is None:
                continue

            catch_up = self._catch_up(current_song, recovered)

            # An expired recovered song must not be started again, even
            # briefly, before advancing to the next queue entry.
            if catch_up is None or catch_up >= 0:
                try:
                    self.player().start_song(current_song, catch_up)
                except PlaybackError:
                    # when a song can't be started, pause the playback
                    # and have the user restart playback manually after fixing the error
                    musiq.controller._pause()
                    musiq.update_state()
                    continue
                redis.put("playing", True)
                if not redis.get("paused") and self.player().has_started_playback():
                    from core import played_history
                    played_history.mark_started(current_song)

                musiq.update_state()
                if not self._wait_until_song_end():
                    # there was an error while waiting for the song to end
                    # This happens when we could not connect to mopidy (ConnectionError)
                    # or when an interrupting alarm was initiated.
                    # we do not delete the current song but recover its state by restarting the loop
                    storage.put("paused", False)
                    redis.put("paused", False)
                    redis.put("playing", False)
                    continue
            # Allowing new songs to start playing while paused introduces many edge cases
            # Instead of dealing with them, always start playback when skipping a paused song
            storage.put("paused", False)
            redis.put("paused", False)
            redis.put("playing", False)

            from core import queue_lock
            with transaction.atomic():
                queue_lock.acquire()
                latest = models.CurrentSong.objects.filter(pk=current_song.pk).first()
                if latest is not None:
                    current_song = latest
                    from core import played_history, voting
                    played_history.record_finished(latest)
                    voting.retire(latest.occurrence_id)
                    latest.delete()
                queue.promote_oldest_extra(current_song.requester_token)

            self._song_finished(current_song)
            playback_state_backup.snapshot()

            if runtime_restart.record_completed_song():
                try:
                    self.player().skip()
                except Exception:  # pylint: disable=broad-except
                    logging.exception("failed to stop player before planned web restart")

                redis.put("playing", False)
                redis.put("backup_playing", False)
                runtime_restart.request_between_songs_restart()


@app.task
def _loop() -> None:
    try:
        playback = Playback()
        playback.loop()
    except Exception:  # pylint: disable=broad-except
        # HTTP can remain responsive after this background task dies. Let the
        # supervisor detect that failure without treating idle/paused playback
        # as unhealthy. The persisted current song and queue remain available.
        logging.exception("playback worker exited unexpectedly")
        redis.put("playback_worker_failed", True)
        raise
    finally:
        connection.close()


@app.task
def _handle_buzzer() -> None:
    try:
        import gpiozero
    except ModuleNotFoundError:
        return
    buzzer = gpiozero.Button(16)

    def on_press():
        trigger_alarm()

    buzzer.when_pressed = on_press

    # wait until this task is told to exit
    buzzer_stopped.wait()


def trigger_alarm() -> bool:
    """Initiate an alarm."""
    # do not allow the alarm to be triggered too frequently
    now = time.time()
    if (now - redis.get("last_buzzer")) < storage.get("buzzer_cooldown"):
        logging.warning("alarm triggered too quickly")
        return False
    # do not allow an alarm to be triggered while one is already playing
    # or when an alarm is currently in the process of being played
    if redis.get("alarm_playing") or redis.get("alarm_requested"):
        logging.warning("last alarm not yet finished")
        return False
    redis.put("last_buzzer", now)
    if redis.get("playing"):
        # if a song is currently playing, inform the loop waiting for the song to end
        # about this alarm. It will interrupt the current song and play the alarm
        redis.put("alarm_requested", True)
    else:
        # insert a special queue song to wake up the main loop and make it play the alarm
        queue.enqueue(musiq.get_alarm_metadata(), True)
        queue_changed.set()
    return True


def handle_autoplay(url: Optional[str] = None) -> None:
    """Checks whether to add a song by autoplay and does so if necessary.
    :param url: if given, this url is used to find the next autoplayed song.
    Otherwise, the current song is used."""
    if not site_mode.accepts_song_requests():
        return
    if storage.get("autoplay") and models.QueuedSong.objects.count() == 0:
        if url is None:
            # if no url was specified, use the one of the current song
            try:
                current_song = models.CurrentSong.objects.get()
                url = current_song.external_url
            except (
                models.CurrentSong.DoesNotExist,
                models.CurrentSong.MultipleObjectsReturned,
            ):
                return

        from core.musiq.song_provider import SongProvider

        provider = SongProvider.create(external_url=url)
        try:
            suggestion = provider.get_suggestion()
            # The player loop is not restarted after error automatically.
            # As this function can raise several exceptions (it might do networking)
            # we catch every exception to make sure the loop keeps running
        except Exception as error:  # pylint: disable=broad-except
            logging.exception("error during suggestions for %s: %s", url, error)
        else:
            if not site_mode.accepts_song_requests():
                return
            provider = SongProvider.create(external_url=suggestion)
            from core.musiq.music_provider import ProviderError
            try:
                provider.request("", archive=False, manually_requested=False)
            except ProviderError:
                if site_mode.accepts_song_requests():
                    raise


def stop() -> None:
    """Stops the playback main loop, only used for tests."""
    redis.put("stop_playback_loop", True)
    queue_changed.set()

