# memo.py — the small piece of st.cache_data this folder still needed.
#
# The pit dashboard is now the React/FastAPI app in Pit_Web/, and nothing it
# imports uses Streamlit. Two of those modules (strategy_engine,
# weather_service) came from the earlier Streamlit dashboard, where they used
# @st.cache_data purely as a memoiser. Keeping that would have meant installing
# Streamlit into a web worker for a decorator — and, outside a Streamlit
# runtime, logging "No runtime found, using MemoryCacheStorageManager" on every
# single call.
#
# This is that decorator, with the two behaviours those callers actually rely on:
#
#   * memoise on the arguments, optionally with a TTL
#   * DO NOT memoise an exception. weather_service depends on this explicitly:
#     it puts the try/except OUTSIDE the cached function so a single dropped
#     packet cannot blank the weather tab for a full hour. st.cache_data behaves
#     the same way, and a naive cache that stored the failure would silently
#     reintroduce that bug.
#
# Deliberately tiny and dependency-free. If this folder ever needs real caching,
# it belongs in the backend, not here.

import functools
import threading
import time


def memo(ttl=None):
    """Memoise on the call arguments, for `ttl` seconds (None = forever).

    A raised exception is propagated and NOT stored, so the next call retries.
    """
    def decorate(fn):
        cache = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            with lock:
                hit = cache.get(key)
                if hit is not None and (hit[0] is None or hit[0] > now):
                    return hit[1]
            # Computed outside the lock: these calls do file and network I/O,
            # and holding a lock across them would serialise every viewer.
            value = fn(*args, **kwargs)
            with lock:
                cache[key] = (None if ttl is None else now + ttl, value)
            return value

        wrapper.clear = cache.clear
        return wrapper
    return decorate
