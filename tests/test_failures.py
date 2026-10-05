"""Tests for cross-run API failure tracking."""
from __future__ import annotations

import asyncio
from pathlib import Path

from or_engine.spatial.failures import FailureRecord, FailureTracker
from or_engine.spatial.api_error import AmapAPIError, _NON_RETRYABLE


def test_failure_record_as_dict():
    r = FailureRecord(category="geocode", key="addr|city",
                      reason="bad key", retryable=False)
    assert r.category == "geocode"
    assert not r.retryable
    d = r.as_dict()
    assert d["category"] == "geocode"
    assert d["key"] == "addr|city"
    assert not d["retryable"]


def test_tracker_record_and_lookup():
    t = FailureTracker()
    t.record(category="geocode", key="addr",
             reason="bad key", retryable=False, infocode="40001")
    assert len(t) == 1
    assert t.is_blocked("geocode", "addr")
    assert not t.is_blocked("geocode", "other")


def test_tracker_persist_roundtrip(tmp_path):
    t = FailureTracker()
    t.record(category="geocode", key="addr|",
             reason="bad key", retryable=False, infocode="40001")
    p = tmp_path
    t.save_path(p)  # saves to p/api_failures.json
    t2 = FailureTracker.load_path(p)
    assert len(t2) == 1
    assert t2.is_blocked("geocode", "addr|")


def test_amapapierror_fields():
    err = AmapAPIError("bad", infocode="40001",
                       http_status=200, retryable=False,
                       request_url="test-url")
    assert err.infocode == "40001"
    assert err.http_status == 200
    assert not err.retryable
    assert err.request_url == "test-url"


def test_non_retryable_infocodes():
    assert "40001" in _NON_RETRYABLE
    assert "40002" in _NON_RETRYABLE
    assert "40003" in _NON_RETRYABLE
    assert "11001" in _NON_RETRYABLE
    assert "20001" in _NON_RETRYABLE
    # transient should NOT be in non-retryable set
    assert "10003" not in _NON_RETRYABLE
    assert "10009" not in _NON_RETRYABLE


def test_tracker_clear():
    t = FailureTracker()
    t.record(category="x", key="k", reason="r", retryable=False)
    t.record(category="y", key="k2", reason="r2", retryable=True)
    assert len(t) == 2
    t.clear()
    assert len(t) == 0


def test_tracker_clear_retryable():
    t = FailureTracker()
    t.record(category="x", key="k", reason="r", retryable=False)
    t.record(category="y", key="k2", reason="r2", retryable=True)
    t.clear_retryable()
    assert len(t) == 1
    assert t.retryable_count() == 0
    assert t.blocked_count() == 1


def test_summary():
    t = FailureTracker()
    t.record(category="geocode", key="addr1|", reason="bad key",
             retryable=False, infocode="40001")
    t.record(category="geocode", key="addr2|", reason="quota",
             retryable=True, infocode="10003")
    s = t.summary()
    assert isinstance(s, str)
    assert "2" in s   # 2 total
    assert t.retryable_count() == 1
    assert t.blocked_count() == 1


if __name__ == "__main__":
    test_failure_record_as_dict()
    test_tracker_record_and_lookup()
    test_non_retryable_infocodes()
    test_amapapierror_fields()
    test_tracker_clear()
    test_tracker_clear_retryable()
    test_summary()
    print("All failure tracking tests passed.")
