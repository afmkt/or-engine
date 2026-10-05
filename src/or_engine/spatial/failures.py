"""spatial/failures.py — persistent API-failure tracking for cross-run replay.

When a geocode or direction call fails, the *nature* of the failure determines
whether the next CLI run should retry it:

* **Non-retryable** (bad API key, bad parameters, geocode miss, IP mismatch,
  key expired): recorded and **skipped** on replay — re-hitting the same error
  wastes time with no chance of success.
* **Retryable** (quota exhausted, QPS/CUQPS throttle, transient server error,
  network timeout): recorded for diagnostics but **always re-attempted** on
  the next run since the underlying condition may have cleared.

Failures are persisted as a single ``api_failures.json`` in the --workdir and
are part of the pipeline checkpoint, so they survive process restarts.

The user can wipe the failure table with ``--clean-failures`` when the
root-cause blocker has been resolved (e.g. rotated the API key).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
#  FailureRecord — one entry per failed API call
# --------------------------------------------------------------------------- #

@dataclass
class FailureRecord:
    """One recorded API-call failure.

    Fields
    ──────
    category  "geocode" | "direction"
       key    natural key for the call:
            geocode   →  "address|city"
            direction →  "from_str|to_str|mode"
       infocode  AMap numeric infocode (str) or None for HTTP / network errors
    reason    human-readable one-line explanation (e.g. "invalid API key")
       retryable  whether the error kind *can* ever succeed on retry
       attempts  running count of how many times this key was attempted
    ts       ISO-8601 timestamp of *this* record (set to last attempt time)
       message  truncated original error text (for debugging / logs)
    """

    category: str
    key: str
    infocode: str | None = None
    reason: str = ""
    retryable: bool = True
    attempts: int = 1
    ts: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message: str = ""

    # ------------------------------------------------------------------ #
    #  serialisation
    # ------------------------------------------------------------------ #
    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FailureRecord":
        # tolerate older / partial payloads
        return cls(
            category=d.get("category", "unknown"),
            key=d.get("key", ""),
            infocode=d.get("infocode"),
            reason=d.get("reason", ""),
            retryable=d.get("retryable", True),
            attempts=d.get("attempts", 1),
            ts=d.get("ts", datetime.now(timezone.utc).isoformat()),
            message=d.get("message", ""),
        )

    # ------------------------------------------------------------------ #
    #  display
    # ------------------------------------------------------------------ #
    def fmt(self) -> str:
        tag = "RETRYABLE" if self.retryable else "NOT-RETRYABLE"
        code = f"infocode={self.infocode}" if self.infocode else "code=HTTP/network"
        return (f"     [{self.category:>9}] {self.key!r:<50s}  "
                f"{code:<20s}  {tag:<12s}  "
                f"reason='{self.reason}'  attempts={self.attempts}")


# --------------------------------------------------------------------------- #
#  FailureTracker — dict-of-FailureRecord, keyed by f"{category}:{key}"
# --------------------------------------------------------------------------- #

def _compose_key(category: str, key: str) -> str:
    return f"{category}:{key}"


class FailureTracker:
    """In-memory + JSON-persisted table of API-call failures.

    Designed to be embedded as ``State.failures`` in the CLI pipeline so
    it is automatically checkpointed and loaded across runs.

    Non-retryable records are *blocking* — callers should check the tracker
    before issuing a new API call and skip the call if a blocking entry
    exists.  Retryable entries are informational; they should be re-attempted.
    """

    def __init__(self, records: dict[str, FailureRecord] | None = None) -> None:
        self._records: dict[str, FailureRecord] = dict(records or {})

    # ------------------------------------------------------------------ #
    #  core lookups
    # ------------------------------------------------------------------ #
    def lookup(self, category: str, key: str) -> FailureRecord | None:
        """Return the most recent failure record for (*category*, *key*), or None."""
        return self._records.get(_compose_key(category, key))

    def is_blocked(self, category: str, key: str) -> bool:
        """True if there is a *non-retryable* (blocking) failure for this call.

        Callers should use this as a precondition gate before making the
        API call, so non-retryable failures are never re-attempted.
        """
        rec = self.lookup(category, key)
        return rec is not None and not rec.retryable

    # ------------------------------------------------------------------ #
    #  recording
    # ------------------------------------------------------------------ #
    def record(
        self,
        category: str,
        key: str,
        *,
        infocode: str | None = None,
        reason: str = "",
        retryable: bool = True,
        message: str = "",
    ) -> FailureRecord:
        """Insert or update a failure record, bumping *attempts* on each call.

        On update, *timestamp* advances but *retryable* is only changed if the
        new classification differs (i.e. we allow a *more-severe* observation
        to override an older softer one, never vice-versa).
        """
        composed = _compose_key(category, key)
        existing = self._records.get(composed)
        if existing is not None:
            # keep the more pessimistic (blocking) classification; bump attempts
            new_retryable = existing.retryable and retryable   # AND → stricter
            existing.attempts += 1
            existing.ts = datetime.now(timezone.utc).isoformat()
            existing.reason = reason or existing.reason
            existing.infocode = infocode or existing.infocode
            existing.message = (message or existing.message)[:500]
            existing.retryable = new_retryable
            return existing
        rec = FailureRecord(
            category=category,
            key=key,
            infocode=infocode,
            reason=reason,
            retryable=retryable,
            attempts=1,
            message=(message or "")[:500],
        )
        self._records[composed] = rec
        return rec

    # ------------------------------------------------------------------ #
    #  removal / reset
    # ------------------------------------------------------------------ #
    def clear(self) -> int:
        """Wipe all records.  Returns the number of entries removed."""
        n = len(self._records)
        self._records.clear()
        return n

    def clear_retryable(self) -> int:
        """Remove only the retryable (non-blocking) entries — the caller has
        presumably re-attempted them since.  Returns count removed."""
        keys = [k for k, r in self._records.items() if r.retryable]
        for k in keys:
            del self._records[k]
        return len(keys)

    # ------------------------------------------------------------------ #
    #  iteration / summary
    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self._records.values())

    def blocked_count(self) -> int:
        return sum(1 for r in self._records.values() if not r.retryable)

    def retryable_count(self) -> int:
        return sum(1 for r in self._records.values() if r.retryable)

    def summary(self) -> str:
        """One-line human-readable summary for CLI output."""
        total = len(self._records)
        if total == 0:
            return "no API failures recorded"
        nb = self.blocked_count()
        nr = self.retryable_count()
        return f"{total} failure record(s)  [blocked/non-retryable: {nb},  " \
               f"retryable: {nr}]"

    def print_replay_summary(self) -> None:
        """Multi-line replay summary, printed at the start of a geocode/matrix step."""
        total = len(self._records)
        if total == 0:
            print(
                "            [replay] no recorded API failures (first run)",
                flush=True,
            )
            return
        nb = self.blocked_count()
        nr = self.retryable_count()
        print(
            f"            [replay] {total} failure(s) loaded from checkpoint   "
            f"→  {nb} non-retryable (skip), {nr} retryable (re-attempt)",
            flush=True,
        )
        # Show non-retryable (the important ones) with a short preview
        blocked = [r for r in self._records.values() if not r.retryable]
        if blocked:
            print(f"            [skip] non-retryable failures — skipped on every run:",
                  flush=True)
            for r in blocked[:10]:       # cap at 10 lines
                print("                  " + r.fmt(), flush=True)
            if len(blocked) > 10:
                print(f"                  … +{len(blocked) - 10} more",
                      flush=True)
        if nr > 0:
            print(f"            [retry] {nr} retryable failure(s) — will re-attempt "
                  f"(quota / transient / network)", flush=True)

    # ------------------------------------------------------------------ #
    #  JSON persistence
    # ------------------------------------------------------------------ #
    def to_json(self) -> dict[str, dict[str, Any]]:
        """Serialise to a JSON-friendly dict for checkpoint I/O."""
        out: dict[str, dict[str, Any]] = {}
        for composed, rec in self._records.items():
            out[composed] = rec.as_dict()
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> "FailureTracker":
        """Deserialise from a dict previously produced by :meth:`to_json`."""
        if not data:
            return cls()
        records: dict[str, FailureRecord] = {}
        for composed, raw in data.items():
            if isinstance(raw, dict):
                records[composed] = FailureRecord.from_dict(raw)
                # fix category from the composed key if the record body is
                # missing it (backward compat with older payloads)
                if not records[composed].category or records[composed].category == "unknown":
                    parts = composed.split(":", 1)
                    if len(parts) == 2:
                        records[composed].category = parts[0]
        return cls(records)

    @classmethod
    def load_path(cls, path: Path) -> "FailureTracker":
        """Load from ``<path>/api_failures.json`` if it exists; empty otherwise."""
        p = Path(path) / "api_failures.json"
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return cls()
        return cls.from_json(data)

    def save_path(self, path: Path) -> None:
        """Write to ``<path>/api_failures.json``.  Creates the dir if needed."""
        p = Path(path) / "api_failures.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
