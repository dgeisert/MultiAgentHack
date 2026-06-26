from __future__ import annotations

import functools
import time


def log(agent: str, msg: str) -> None:
    print(f"  [{agent}] {msg}", flush=True)


def retry(times: int = 3, base_delay: float = 1.0, exc=(Exception,)):
    """Exponential-backoff retry decorator for flaky external calls."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            last = None
            for attempt in range(times):
                try:
                    return fn(*args, **kwargs)
                except exc as e:  # noqa: BLE001
                    last = e
                    if attempt < times - 1:
                        time.sleep(base_delay * (2**attempt))
            raise last  # type: ignore[misc]

        return wrapped

    return deco
