"""This module contains all url endpoints and maps them to their corresponding functions."""
import inspect
from typing import Any, List, Union

from django.contrib.auth.views import LoginView
from django.core.handlers.wsgi import WSGIRequest
from django.urls import URLPattern, URLResolver, include, path
from django.views.generic import RedirectView

from core import api
from core import base
from core import moderation
from core.lights import controller as lights_controller
from core.lights import lights
from core.musiq import controller as musiq_controller
from core.musiq import musiq
from core.musiq import suggestions
from core import network_info
from core.settings import analysis
from core.settings import basic
from core.settings import library
from core.settings import platforms
from core.settings import settings
from core.settings import sound
from core.settings import system
from core.settings import wifi
from core import state_handler
from core import ui_notifications
from core import visualizer
from core.authentication import ModeratorAuthenticationForm

urlpatterns: List[Union[URLPattern, URLResolver]] = [
    path("visualizer/background/", visualizer.background, name="visualizer-background"),
    path("visualizer/features.json", visualizer.features, name="visualizer-features"),
    path("", base.landing, name="base"),
    path("afterhours/", base.afterhours, name="afterhours"),
    path("afterhours/top-songs/", base.afterhours_top_songs, name="afterhours-top-songs"),
    path("afterhours/history/", base.afterhours_history, name="afterhours-history"),
    path("p/", musiq.embed, name="musiq"),
    path("musiq/", RedirectView.as_view(pattern_name="base", permanent=False)),
    path("lights/", lights.index, name="lights"),
    path("stream/", base.no_stream, name="no-stream"),
    path("network-info/", network_info.index, name="network-info"),
    path("settings/", base.settings_disabled, name="settings"),
    path("moderator/", moderation.dashboard, name="moderator"),
    path(
        "moderator-login/",
        LoginView.as_view(
            template_name="furatic_login.html",
            authentication_form=ModeratorAuthenticationForm,
            redirect_authenticated_user=True,
            next_page="moderator",
        ),
        name="furatic-login",
    ),
    path("accounts/", include("django.contrib.auth.urls")),
    path("login/", RedirectView.as_view(pattern_name="login", permanent=False)),
    path("logged-in/", base.logged_in, name="logged-in"),
    path("logout/", RedirectView.as_view(pattern_name="logout", permanent=False)),
    path(
        "api/",
        include(
            [
                path("version/", api.version, name="version"),
                path("site-mode/", base.site_mode_status, name="site-mode-status"),
                path("notifications/", ui_notifications.notifications, name="ui-notifications"),
                path(
                    "moderator/",
                    include(
                        [
                            path("state/", moderation.state, name="moderator-state"),
                            path(
                                "remove-song/",
                                moderation.remove_song,
                                name="moderator-remove-song",
                            ),
                            path(
                                "skip-current/",
                                moderation.skip_current_song,
                                name="moderator-skip-current",
                            ),
                            path("ban-ip/", moderation.ban_ip, name="moderator-ban-ip"),
                            path(
                                "unban-ip/",
                                moderation.unban_ip,
                                name="moderator-unban-ip",
                            ),
                            path(
                                "whitelist-ip/",
                                moderation.whitelist_ip,
                                name="moderator-whitelist-ip",
                            ),
                            path(
                                "unwhitelist-ip/",
                                moderation.unwhitelist_ip,
                                name="moderator-unwhitelist-ip",
                            ),
                            path(
                                "site-mode/",
                                moderation.set_site_mode,
                                name="moderator-site-mode",
                            ),
                            path(
                                "request-settings/",
                                moderation.set_request_settings,
                                name="moderator-request-settings",
                            ),
                            path(
                                "blocklists/add/",
                                moderation.add_blocklist,
                                name="moderator-add-blocklist",
                            ),
                            path(
                                "blocklists/rename/",
                                moderation.rename_blocklist,
                                name="moderator-rename-blocklist",
                            ),
                            path(
                                "blocklists/remove/",
                                moderation.remove_blocklist,
                                name="moderator-remove-blocklist",
                            ),
                            path("accounts/save/", moderation.save_moderator_account, name="moderator-account-save"),
                            path("accounts/delete/", moderation.delete_moderator_account, name="moderator-account-delete"),
                            path("identity-search/", moderation.identity_search, name="moderator-identity-search"),
                            path("audit-search/", moderation.audit_search, name="moderator-audit-search"),
                            path("review-song/", moderation.review_song, name="moderator-review-song"),
                            path("song-details/", moderation.song_details, name="moderator-song-details"),
                        ]
                    ),
                ),
                path(
                    "musiq/",
                    include(
                        [
                            path("post_song/", api.post_song, name="post_song"),
                            path("post-song/", api.post_song, name="post-song"),
                        ]
                    ),
                ),
            ]
        ),
    ),
]

urlmethods = [urlpattern.callback for urlpattern in urlpatterns]


def get_paths(objs: List[Any]) -> List[URLPattern]:
    """Iterates through the given objects and identifies all methods that serve http requests.
    A url pattern is generated for each of these methods,
    but only if no such path exists already in urlpatterns.
    Returns the list of url patterns."""
    paths = []
    for obj in objs:
        for name, method in inspect.getmembers(obj, inspect.isfunction):
            if name == "get_state":
                # the state url is an exception
                # it cannot be named after its method name, as every page has its own get_state
                continue
            if name.startswith("_"):
                # do not expose internal methods
                continue
            # use __annotations__ instead of get_type_hints, because the latter raised errors about
            # WSGIRequest not being defined in some configurations
            # string annotations are sufficient for our use case
            type_hints = method.__annotations__
            if "request" in type_hints:
                request_type = type_hints["request"]
            elif "_request" in type_hints:
                request_type = type_hints["_request"]
            else:
                continue
            if (
                request_type == "WSGIRequest" or issubclass(request_type, WSGIRequest)
            ) and method not in urlmethods:
                name = name.replace("_", "-")
                paths.append(path(name + "/", method, name=name))
    return paths


base_paths = get_paths([base])
musiq_paths = get_paths([musiq, musiq_controller, suggestions])
lights_paths = get_paths([lights_controller])
settings_paths = get_paths([basic, platforms, sound, wifi, library, analysis, system])

urlpatterns.append(
    path(
        "ajax/",
        include(
            [
                path("", include(base_paths)),
                path(
                    "musiq/state/",
                    state_handler.get_state,
                    {"module": musiq},
                    name="musiq-state",
                ),
                path("musiq/", include(musiq_paths)),
                path(
                    "lights/state/",
                    state_handler.get_state,
                    {"module": lights},
                    name="lights-state",
                ),
                path("lights/", include(lights_paths)),
            ]
        ),
    )
)
