"""api_error.py -- AmapAPIError + failure record helpers."""

from __future__ import annotations

class AmapAPIError(RuntimeError):
    def __init__(self, msg, *, infocode=None, http_status=None,
                 retryable=True, request_url=""):
        super().__init__(msg)
        self.message = msg
        self.infocode = infocode
        self.http_status = http_status
        self.retryable = retryable
        self.request_url = request_url


_NON_RETRYABLE = frozenset({
        "40001", "40002", "40003", "40006", "40008",
        "20001", "11001",
})


def record_geocode_failure(tracker, addr, city, exc):
    if tracker is None or getattr(exc, "retryable", True): return
    tracker.record(category="geocode", key=f"{addr}|{city}",
         reason=str(exc.infocode or "unknown"),
         retryable=False, infocode=exc.infocode,
         http_status=exc.http_status, request_url=exc.request_url)

def record_direction_failure(tracker, from_str, to_str, mode, exc):
    if tracker is None or getattr(exc, "retryable", True): return
    tracker.record(category="direction",
         key=f"{from_str}|{to_str}|{mode}",
         reason=str(exc.infocode or "unknown"),
         retryable=False, infocode=exc.infocode,
         http_status=exc.http_status, request_url=exc.request_url)
