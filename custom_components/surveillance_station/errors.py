"""The WebSocket commands' own refusals: answered to the card, never logged.

Their own classes, so that a KeyError or ValueError from a bug still reaches
HA's handler (logged, with its traceback) instead of passing for one of these.
"""

from __future__ import annotations


class EntryNotLoaded(Exception):
    """No Surveillance Station entry is loaded, or not the one asked for (not_found)."""


class SessionNotFound(Exception):
    """A token names no recordings session: expired, never issued, or a time-lapse's (not_found)."""


class InvalidRequest(ValueError):
    """What was asked can't be answered as asked (invalid_format)."""
