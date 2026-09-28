"""AirPlay state aggregation from shairport-sync's D-Bus snapshot.

ShairportLink (shairport.py) hands over one snapshot per change: whether
shairport-sync is on the bus, its Active flag, the RemoteControl PlayerState
("Playing" / "Paused" / "Stopped" / "Not Available"), ClientName and the
MPRIS-style Metadata dict. This module folds that into one now_playing dict;
the app publishes it and recomputes the active-source sensor.
"""
from __future__ import annotations

import logging

log = logging.getLogger("speakerd.airplay")


def _text(v) -> str | None:
    if isinstance(v, (list, tuple)):  # xesam:artist is a string list
        v = ", ".join(str(x) for x in v if x)
    if v is None:
        return None
    return str(v).strip() or None


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

    def apply(self, snap: dict) -> bool:
        """Fold a ShairportLink snapshot. Returns True when the folded state
        materially changed."""
        before = self._key()
        present = bool(snap.get("present"))
        self.playing = present and snap.get("player_state") == "Playing"
        # a playing session is active even if the Active edge hasn't landed yet
        self.active = present and (bool(snap.get("active")) or self.playing)
        if self.active:
            md = snap.get("metadata") or {}
            self.title = _text(md.get("xesam:title"))
            self.artist = _text(md.get("xesam:artist"))
            self.album = _text(md.get("xesam:album"))
            self.client = _text(snap.get("client_name"))
        else:
            # shairport keeps the last track's metadata after a session ends;
            # an idle node must not keep showing it
            self._clear_track()
            self.client = None
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
