"""Durable, shared model cooldowns. A deferred request never sleeps in a worker."""
import math
import random
import time
import threading
from datetime import timezone
from email.utils import parsedate_to_datetime


class CapacityDelayed(RuntimeError):
    def __init__(self, model, state):
        super().__init__('Waiting for model capacity')
        self.model = model
        self.state = state


def retry_delay(headers):
    for name, scale in (('retry-after-ms', .001), ('retry-after', 1)):
        value = headers.get(name)
        if value is None:
            continue
        try:
            seconds = float(value) * scale
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                seconds = date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


class Capacity:
    _lock = threading.RLock()
    def __init__(self, store):
        self.store = store

    def get(self, model):
        return self.store.state('capacity:' + model, {})

    def check(self, model):
        state = self.get(model)
        if state.get('started') is not None and time.time() >= state.get('deadline',float('inf')):
            state['paused'] = True
            self.store.set_state('capacity:' + model,state)
        if state.get('paused') or state.get('retry_at', 0) > time.time():
            raise CapacityDelayed(model, state)

    def limited(self, model, headers, detail, window):
        with self._lock, self.store.lock:
            state = self.get(model)
            stamp = time.time()
            count = state.get('attempts', 0) + 1
            delay = retry_delay(headers)
            if delay is None:
                delay = min(60, 5 * 2 ** min(count - 1, 4)) * random.uniform(1, 1.2)
            started = state.get('started', stamp)
            state = {'started': started, 'deadline': started + window, 'attempts': count,
                     'retry_at': max(state.get('retry_at', 0), stamp + max(1, delay)),
                     'paused': stamp - started >= window, 'detail': detail}
            self.store.set_state('capacity:' + model, state)
            return CapacityDelayed(model, state)

    def succeeded(self, model, observed=None):
        with self._lock, self.store.lock:
            # An older in-flight success must not erase a newer 429 cooldown.
            if observed is not None and self.get(model) != observed:
                return
            self.store.set_state('capacity:' + model, {})

    def reset(self, model):
        self.succeeded(model)
