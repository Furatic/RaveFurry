"""Render the real player for local tutorial practice, without live state."""
from django.shortcuts import render
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET

from core import base
from core.settings import storage


@require_GET
@xframe_options_sameorigin
def player(request):
    lesson = request.GET.get("lesson", "adding")
    if lesson not in {"adding", "voting", "multiple", "prequeue"}:
        lesson = "adding"
    # Do not call base.context(): practice must not increment visitor counters,
    # inspect personal votes, or read/mutate the event queue.
    context = {
        "tutorial_lesson": lesson,
        "interactivity": storage.Interactivity.full_voting,
        "interactivities": {
            "fullControl": storage.Interactivity.full_control,
            "fullVoting": storage.Interactivity.full_voting,
            "upvotesOnly": storage.Interactivity.upvotes_only,
            "noControl": storage.Interactivity.no_control,
        },
        "controls_enabled": False,
        "color_indication": False,
        "client_streaming": False,
        "show_stream": False,
        "youtube_enabled": True,
        "static_asset_version": base._static_asset_version(),
    }
    response = base._disable_dynamic_page_cache(render(request, "tutorial_player.html", context))
    # Enforce isolation even if a future shared UI accidentally adds a request.
    response["Content-Security-Policy"] = "connect-src 'none'; media-src 'none'; form-action 'none'; frame-ancestors 'self'"
    return response
