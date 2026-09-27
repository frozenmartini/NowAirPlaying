"""AirPlay state aggregation from shairport-sync's MQTT topics.

shairport-sync (publish_parsed) publishes one value per subtopic under
<base>/airplay/: title, artist, album, client_name, active, playing, and
event topics play_start/play_end/play_resume/play_flush/active_start/
active_end. This module folds them into one now_playing dict; the app
publishes it retained and recomputes the active-source sensor.
"""
from __future__ import annotations

import logging

log = logging.getLogger("speakerd.airplay")

_TRUTHY = {"1", "true", "yes", "on"}

# subtopics we fold in; everything else (cover art, format, volume, …) is ignored
_TEXT_FIELDS = {"title", "artist", "album", "client_name"}
_IGNORED = {"now_playing", "remote"}  # our own output / command channel
# edge-triggered event topics: meaningful live, stale when replayed as retained
_EVENTS = {"active_start", "active_end", "play_start", "play_end",
           "play_resume", "play_flush"}


class AirplayState:
    def __init__(self):
        self.title: str | None = None
        self.artist: str | None = None
        self.album: str | None = None
        self.client: str | None = None
        self.active = False
        self.playing = False

    @property
    def status(self) -> str:
        if self.active and self.playing:
            return "playing"
        if self.active:
            return "paused"
        return "idle"

    def handle(self, subtopic: str, payload: bytes, retained: bool = False) -> bool:
        """Returns True when the folded state materially changed."""
        if subtopic in _IGNORED:
            return False
        if retained and subtopic in _EVENTS:
            # retained replay rehydrates only level state (active/playing/title/…)
            return False
        before = self._key()
        text = payload.decode("utf-8", errors="replace").strip()
        if text == "--":  # shairport's empty_payload_substitute
            text = ""

        if subtopic in _TEXT_FIELDS:
            value = text or None
            if subtopic == "client_name":
                self.client = value
            else:
                setattr(self, subtopic, value)
        elif subtopic == "active":
            self.active = text.lower() in _TRUTHY
            if not self.active:
                self._clear_track()
        elif subtopic == "playing":
            self.playing = text.lower() in _TRUTHY
        elif subtopic == "active_start":
            self.active = True
        elif subtopic == "active_end":
            self.active = False
            self.playing = False
            self._clear_track()
        elif subtopic in ("play_start", "play_resume"):
            self.active = True
            self.playing = True
        elif subtopic == "play_end":
            self.playing = False
        # play_flush fires on seeks/track changes; shairport keeps 'playing' — so do we
        else:
            return False

        return self._key() != before

    def _clear_track(self) -> None:
        self.title = self.artist = self.album = None

    def _key(self) -> tuple:
        return (self.status, self.title, self.artist, self.album, self.client)

    def now_playing(self) -> dict:
        return {
            "status": self.status,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "client": self.client,
        }
