"""Websocket routes."""

from django.urls import re_path

from core.state_handler import StateConsumer
from core.visualizer import VisualizerConsumer

WEBSOCKET_URLPATTERNS = [
    re_path(r"^visualizer/features/$", VisualizerConsumer.as_asgi()),
    re_path(r"state/$", StateConsumer.as_asgi()),
]

# Backward-compatible alias.
websocket_urlpatterns = WEBSOCKET_URLPATTERNS
