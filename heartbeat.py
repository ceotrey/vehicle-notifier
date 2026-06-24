"""
heartbeat.py

Tiny thread-safe registry so each polling loop can report it completed a cycle.
The health monitor reads these timestamps to detect a thread that is technically
alive (thread.is_alive() == True) but stuck/wedged — something the main.py
watchdog cannot catch on its own.

Usage:
    import heartbeat
    heartbeat.beat("dm-bot")          # call at the end of each poll cycle
    heartbeat.last_beat("dm-bot")     # -> float timestamp or None
"""

import time
import threading

_beats: dict = {}   # {thread_name: last_success_timestamp}
_lock = threading.Lock()


def beat(name: str):
    """Record that the named loop just completed a cycle."""
    with _lock:
        _beats[name] = time.time()


def last_beat(name: str):
    """Return the last heartbeat timestamp for a thread, or None if never seen."""
    with _lock:
        return _beats.get(name)


def all_beats() -> dict:
    """Return a snapshot copy of all heartbeats."""
    with _lock:
        return dict(_beats)
