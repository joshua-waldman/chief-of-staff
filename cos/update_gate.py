"""Transient maintenance barrier; never changes the owner's queue settings."""
import threading
from functools import wraps

lock = threading.RLock()
pending = False
active = 0


def enter():
    global active
    with lock:
        if pending:
            return False
        active += 1
        return True


def leave():
    global active
    with lock:
        active -= 1


def background(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not enter():
            return None
        try:
            return function(*args, **kwargs)
        finally:
            leave()
    return wrapped
