"""This module contains all Youtube related code."""
# We need to access yt-dlp's internal methods for some features
# pylint: disable=protected-access

from __future__ import annotations

import errno
import functools
import logging
import os
import pickle
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, cast
from urllib.parse import parse_qs, urlparse

import requests
import yt_dlp
import ytmusicapi
from django.conf import settings
from django.http.response import HttpResponse

from core.musiq import musiq, song_utils
from core.musiq.playlist_provider import PlaylistProvider
from core.musiq.song_provider import SongProvider
from core.settings import storage


@contextmanager
def youtube_session() -> Iterator[requests.Session]:
    """This context opens a requests session and loads the youtube cookies file."""

    cookies_path = os.path.join(settings.BASE_DIR, "config/youtube_cookies.pickle")
    session = requests.session()
    # Have yt-dlp deal with consent cookies etc to setup a valid session
    extractor = yt_dlp.extractor.youtube.YoutubeIE()
    extractor._downloader = yt_dlp.YoutubeDL()
    extractor.initialize()
    session.cookies.update(extractor._downloader.cookiejar)

    try:
        if os.path.getsize(cookies_path) > 0:
            with open(cookies_path, "rb") as cookies_file:
                session.cookies.update(pickle.load(cookies_file))
    except FileNotFoundError:
        pass

    headers = {"User-Agent": yt_dlp.utils.random_user_agent()}
    session.headers.update(headers)
    yield session

    with open(cookies_path, "wb") as cookies_file:
        pickle.dump(session.cookies, cookies_file)


class YoutubeDLLogger:
    """This logger class is used to log process of yt-dlp downloads."""

    @classmethod
    def debug(cls, msg: str) -> None:
        """This method is called if yt-dlp does debug level logging."""
        logging.debug(msg)

    @classmethod
    def warning(cls, msg: str) -> None:
        """This method is called if yt-dlp does warning level logging."""
        logging.debug(msg)

    @classmethod
    def error(cls, msg: str) -> None:
        """This method is called if yt-dlp does error level logging."""
        logging.error(msg)


YTMUSIC_TIMEOUT_SECONDS = 25
YTDLP_SOCKET_TIMEOUT_SECONDS = 30


def get_ytmusic() -> ytmusicapi.YTMusic:
    """Return a YTMusic client with a short timeout so requests cannot hang forever."""
    session = requests.Session()
    session.request = functools.partial(  # type: ignore[method-assign]
        session.request,
        timeout=YTMUSIC_TIMEOUT_SECONDS,
    )
    return ytmusicapi.YTMusic(requests_session=session)


class Youtube:
    """This class contains code for both the song and playlist provider"""

    used_info_dict_keys = {"id", "filesize", "url", "_type", "title", "entries"}

    @staticmethod
    def get_ydl_opts() -> Dict[str, Any]:
        """This method returns a dictionary containing sensible defaults for yt-dlp options.
        It is roughly equivalent to the following command:
        yt-dlp --format bestaudio[ext=m4a]/best[ext=m4a] --output '%(id)s.%(ext)s' \
            --no-playlist --no-cache-dir --write-thumbnail --default-search auto \
            --add-metadata --embed-thumbnail
        """
        postprocessors = [
            {"key": "FFmpegMetadata"},
            {
                "key": "EmbedThumbnail",
                # overwrite any thumbnails already present
                "already_have_thumbnail": True,
            },
        ]
        return {
            "format": "bestaudio[ext=m4a]/best[ext=m4a]",
            "outtmpl": os.path.join(settings.SONGS_CACHE_DIR, "%(id)s.%(ext)s"),
            "noplaylist": True,
            "cachedir": False,
            "no_color": True,
            "writethumbnail": True,
            "default_search": "auto",
            "postprocessors": postprocessors,
            "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
            "retries": 2,
            "extractor_retries": 2,
            "fragment_retries": 2,
            "logger": YoutubeDLLogger(),
        }

    @staticmethod
    def get_search_suggestions(query: str) -> List[str]:
        """Returns a list of suggestions for the given query from Youtube."""
        try:
            with youtube_session() as session:
                params = {
                    "client": "youtube",
                    "q": query[:100],  # queries longer than 100 characters are not accepted
                    "xhr": "t",  # this makes the response be a json file
                }
                session.get(
                    "https://clients1.google.com/complete/search",
                    params=params,
                    timeout=YTMUSIC_TIMEOUT_SECONDS,
                )

            suggestions = get_ytmusic().get_search_suggestions(query)
        except Exception as error:  # pylint: disable=broad-except
            logging.warning("youtube suggestions failed for %r: %s", query, error)
            return []

        try:
            if suggestions[0] == query:
                suggestions = suggestions[1:]
        except IndexError:
            return []

        return suggestions


class YoutubeSongProvider(SongProvider, Youtube):
    """This class handles songs from Youtube."""

    @staticmethod
    def max_duration_seconds() -> float:
        return max(0.0, float(storage.get("max_song_duration_seconds")))

    @staticmethod
    def _normalize_host(url: str) -> str:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
        if host.startswith("www."):
            host = host[4:]
        return host

    @staticmethod
    def _is_youtube_host(host: str) -> bool:
        return (
            host == "youtu.be"
            or host == "youtube.com"
            or host.endswith(".youtube.com")
            or host.startswith("youtube.")
        )

    @staticmethod
    def get_id_from_external_url(url: str) -> str:
        parsed = urlparse(url)
        host = YoutubeSongProvider._normalize_host(url)

        if host == "youtu.be":
            video_id = parsed.path.strip("/").split("/", 1)[0]
            if video_id:
                return video_id
            raise KeyError("missing youtube video id")

        if YoutubeSongProvider._is_youtube_host(host):
            video_ids = parse_qs(parsed.query).get("v")
            if video_ids and video_ids[0]:
                return video_ids[0]

        raise KeyError("missing youtube video id")

    def __init__(self, query: Optional[str], key: Optional[int]) -> None:
        self.type = "youtube"
        super().__init__(query, key)
        self.info_dict: Dict[str, Any] = {}

    def check_cached(self) -> bool:
        if not self.id:
            # id could not be extracted from query, needs to be serched
            return False
        if storage.get("output") == "client":
            # youtube streaming links need to be fetched each time the song is requested
            return False
        return os.path.isfile(self.get_path()) and os.path.getsize(self.get_path()) > 0

    def check_available(self) -> bool:
        self.info_dict = {}
        self.error = ""
        self.download_rejected = False

        query = self.query or ""

        if not self.id:
            parsed_query = urlparse(query)
            if parsed_query.scheme in {"http", "https"} and parsed_query.netloc:
                host = self._normalize_host(query)

                if not self._is_youtube_host(host):
                    self.error = "Please paste a valid YouTube link."
                    return False

                try:
                    self.id = self.get_id_from_external_url(query)
                except KeyError:
                    self.error = "Please paste a valid YouTube video link."
                    return False

        def is_age_restricted(error: Exception) -> bool:
            message = str(error).lower()
            return (
                "age-restricted" in message
                or "confirm your age" in message
                or "sign in to confirm your age" in message
            )

        def duration_limit_label() -> str:
            return song_utils.format_seconds(self.max_duration_seconds())

        def is_too_long(info: Dict[str, Any]) -> bool:
            max_duration = self.max_duration_seconds()
            duration = info.get("duration")
            return max_duration > 0 and duration is not None and duration > max_duration

        def extract_info(video_id: str) -> bool:
            try:
                with yt_dlp.YoutubeDL(Youtube.get_ydl_opts()) as ydl:
                    info = ydl.extract_info(video_id, download=False) or {}

                if not info:
                    return False

                self.info_dict = info
                self.error = ""
                return True
            except (yt_dlp.utils.ExtractorError, yt_dlp.utils.DownloadError) as error:
                logging.warning("error during availability check for %s:", video_id)
                logging.warning(error)
                if is_age_restricted(error):
                    self.error = "Sorry, this video is age-restricted."
                else:
                    self.download_rejected = True
                return False

        if self.id:
            # do not search if an id is already present
            if not extract_info(self.id):
                if not self.error:
                    self.error = "Sorry, this video could not be played."
                return False

            if is_too_long(self.info_dict):
                self.error = (
                    "Sorry, content over "
                    f"{duration_limit_label()} long is not allowed."
                )
                return False
        else:
            # do not filter to only receive "song" results, because we would skip the top result
            try:
                results = get_ytmusic().search(query)
            except Exception as error:
                logging.warning("ytmusic search failed for %r: %s", query, error)
                try:
                    with yt_dlp.YoutubeDL(Youtube.get_ydl_opts()) as ydl:
                        search_result = ydl.extract_info(
                            f"ytsearch1:{query}",
                            download=False,
                        ) or {}
                    entries = search_result.get("entries") or []
                    for entry in entries:
                        duration = entry.get("duration")
                        if duration is not None and duration > self.MAX_DURATION_SECONDS:
                            continue
                        self.info_dict = entry
                        self.error = ""
                        break
                except (yt_dlp.utils.ExtractorError, yt_dlp.utils.DownloadError) as fallback_error:
                    logging.warning(
                        "yt-dlp fallback search failed for %r: %s",
                        query,
                        fallback_error,
                    )
                    if is_age_restricted(fallback_error):
                        self.error = "Sorry, this video is age-restricted."
                    self.info_dict = {}
            else:
                for result in results:
                    if result["resultType"] not in ("video", "song"):
                        continue
                    if song_utils.is_forbidden(result["title"]):
                        continue
                    if not extract_info(result["videoId"]):
                        continue
                    if is_too_long(self.info_dict):
                        self.info_dict = {}
                        continue
                    break

        if not self.info_dict:
            if not self.error:
                self.error = "Sorry, no playable song was found."
            return False

        self.id = self.info_dict["id"]
        return self.check_not_too_large(self.info_dict.get("filesize"))

    def _download(self) -> bool:
        download_error = None
        location = None
        options = Youtube.get_ydl_opts()
        if getattr(self, "download_incomplete", False):
            options["overwrites"] = True
        self.download_incomplete = True

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                if ydl.download([self.get_external_url()]) != 0:
                    self.error = "Sorry, this video could not be downloaded."
                    return False

            location = self.get_path()
            if not os.path.isfile(location) or os.path.getsize(location) == 0:
                self.error = "Sorry, this video could not be downloaded."
                return False
            base = os.path.splitext(location)[0]
            thumbnail = base + ".jpg"
            try:
                os.remove(thumbnail)
            except FileNotFoundError:
                logging.info("tried to delete %s but does not exist", thumbnail)

            try:
                # tag the file with replaygain to perform volume normalization
                subprocess.call(
                    ["rganalysis", location],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as error:
                if error.errno == errno.ENOENT:
                    pass  # the rganalysis package was not found. Skip normalization
                else:
                    raise

        except yt_dlp.utils.DownloadError as error:
            download_error = error
            message = str(error).lower()
            if (
                "age-restricted" in message
                or "confirm your age" in message
                or "sign in to confirm your age" in message
            ):
                self.error = "Sorry, this video is age-restricted."
            else:
                self.error = "Sorry, this video could not be downloaded."

        if download_error is not None or location is None:
            logging.error("accessible video could not be downloaded: %s", self.id)
            logging.error("location: %s", location)
            logging.error(download_error)
            return False
        self.download_incomplete = False
        return True

    def make_available(self) -> bool:
        if not getattr(self, "download_incomplete", False) and os.path.isfile(self.get_path()) and os.path.getsize(self.get_path()) > 0:
            # don't download the file if it is already cached
            return True
        musiq.update_state()
        return self._download()

    def get_path(self) -> str:
        """Return the path in the local filesystem to the cached sound file of this song."""
        if not self.id:
            raise ValueError()
        return song_utils.get_path(self.id + ".m4a")

    def get_internal_url(self) -> str:
        return Path(self.get_path()).resolve().as_uri()

    def get_external_url(self) -> str:
        if not self.id:
            raise ValueError()
        return "https://www.youtube.com/watch?v=" + self.id

    def gather_metadata(self) -> bool:
        metadata = self.get_local_metadata(self.get_path())

        if not self.info_dict:
            try:
                with yt_dlp.YoutubeDL(Youtube.get_ydl_opts()) as ydl:
                    self.info_dict = (
                        ydl.extract_info(self.get_external_url(), download=False) or {}
                    )
            except (yt_dlp.utils.ExtractorError, yt_dlp.utils.DownloadError) as error:
                logging.warning(
                    "could not refresh youtube metadata for %s:",
                    self.get_external_url(),
                )
                logging.warning(error)
                self.info_dict = {}

        if self.info_dict:
            title = (
                self.info_dict.get("track")
                or self.info_dict.get("title")
                or metadata.get("title")
                or self.get_external_url()
            )
            artist = (
                self.info_dict.get("artist")
                or self.info_dict.get("uploader")
                or self.info_dict.get("channel")
                or metadata.get("artist")
                or ""
            )
            duration = self.info_dict.get("duration") or metadata.get("duration", -1)

            metadata["title"] = title
            metadata["artist"] = artist
            metadata["duration"] = duration
            metadata["artwork_url"] = self.info_dict.get("thumbnail") or ""
            categories = self.info_dict.get("categories") or []
            metadata["genre"] = self.info_dict.get("genre") or (categories[0] if categories else "")

        self.metadata = metadata
        return True

    def get_suggestion(self) -> str:
        result = get_ytmusic().get_watch_playlist(self.id, limit=2)

        # the first entry usually is the song itself -> use the second one
        suggested_id = result["tracks"][1]["videoId"]

        return "https://www.youtube.com/watch?v=" + suggested_id

    def request_radio(self, session_key: str) -> HttpResponse:
        if not self.id:
            raise ValueError()

        result = get_ytmusic().get_watch_playlist(
            self.id, limit=storage.get("max_playlist_items"), radio=True
        )

        radio_id = result["playlistId"]

        provider = YoutubePlaylistProvider("", None)
        provider.id = radio_id
        provider.title = radio_id

        for entry in result["tracks"]:
            provider.urls.append("https://www.youtube.com/watch?v=" + entry["videoId"])

        provider.request("", archive=False, manually_requested=False)

        return HttpResponse("queueing radio (might take some time)")


class YoutubePlaylistProvider(PlaylistProvider, Youtube):
    """This class handles Youtube Playlists."""

    @staticmethod
    def get_id_from_external_url(url: str) -> Optional[str]:
        try:
            list_id = parse_qs(urlparse(url).query)["list"][0]
        except KeyError:
            return None
        return list_id

    def __init__(self, query: Optional[str], key: Optional[int]) -> None:
        self.type = "youtube"
        super().__init__(query, key)

    def is_radio(self) -> bool:
        if not self.id:
            raise ValueError()
        return self.id.startswith("RD")

    def search_id(self) -> Optional[str]:
        results = get_ytmusic().search(self.query)

        for result in results:
            if result["resultType"] not in (
                "playlist",
                "community_playlist",
                "featured_playlist",
            ):
                continue
            if "browseId" not in result or not result["browseId"]:
                continue
            # remove the preceding "VL" from the playlist id
            list_id = result["browseId"][2:]
            return list_id

    def fetch_metadata(self) -> bool:
        assert self.id

        # radio playlists are prefilled when requesting them
        if self.title and self.urls:
            return True

        try:
            result = get_ytmusic().get_playlist(self.id)
        except Exception as error:  # pylint: disable=broad-except
            logging.warning("youtube playlist metadata failed for %s: %s", self.id, error)
            return False

        assert self.id == result["id"]
        self.title = result["title"]
        for entry in result["tracks"]:
            if "videoId" not in entry or not entry["videoId"]:
                continue
            self.urls.append("https://www.youtube.com/watch?v=" + entry["videoId"])
        assert self.key is None

        return True
