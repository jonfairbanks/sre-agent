"""Alert clocks follow explicit pod settings and keep source-date DST rules."""
from datetime import datetime, timezone
import os
import time
from zoneinfo import ZoneInfo

import pytest

from health_evidence import _alert_local_time, _termination_detail


@pytest.fixture(autouse=True)
def clear_timezone_settings(monkeypatch):
    monkeypatch.delenv("ALERT_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)


def detail(stamp):
    return _termination_detail({"container": "app", "reason": "Unknown",
                                "finished_at": stamp, "exit_code": 255, "signal": 9})


def test_alert_timezone_overrides_pod_timezone(monkeypatch):
    monkeypatch.setenv("ALERT_TIMEZONE", "America/New_York")
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert "Oct 10, 2026 at 10:37:15 PM EDT" in detail("2026-10-11T02:37:15Z")


def test_pod_timezone_is_used_without_alert_override(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert "Oct 11, 2026 at 11:37:15 AM JST" in detail("2026-10-11T02:37:15Z")


@pytest.mark.parametrize("name", ["Missing/Zone", "/etc/localtime", "../UTC", " "])
def test_invalid_override_falls_back_to_valid_pod_timezone(monkeypatch, name):
    monkeypatch.setenv("ALERT_TIMEZONE", name)
    monkeypatch.setenv("TZ", "UTC")
    assert "Oct 11, 2026 at 2:37:15 AM UTC" in detail("2026-10-11T02:37:15Z")


@pytest.mark.parametrize("stamp,clock", [
    ("2026-03-08T09:59:00Z", "Mar 8, 2026 at 1:59:00 AM PST"),
    ("2026-03-08T10:01:00Z", "Mar 8, 2026 at 3:01:00 AM PDT"),
    ("2026-11-01T08:59:00Z", "Nov 1, 2026 at 1:59:00 AM PDT"),
    ("2026-11-01T09:01:00Z", "Nov 1, 2026 at 1:01:00 AM PST"),
])
def test_configured_timezone_observes_dst_at_source_instant(monkeypatch, stamp, clock):
    monkeypatch.setenv("ALERT_TIMEZONE", "America/Los_Angeles")
    text = detail(stamp)
    assert clock in text
    assert "exit" not in text.lower() and "signal" not in text.lower()


def test_unconfigured_clock_uses_system_local_timezone():
    source = datetime(2026, 1, 11, 2, 37, 15, tzinfo=timezone.utc)
    assert _alert_local_time(source) == source.astimezone()
    assert _alert_local_time(source).tzname() == source.astimezone().tzname()


def test_invalid_settings_use_system_local_timezone(monkeypatch):
    monkeypatch.setenv("ALERT_TIMEZONE", "Missing/Zone")
    monkeypatch.setenv("TZ", "Missing/OtherZone")
    source = datetime(2026, 1, 11, 2, 37, 15, tzinfo=timezone.utc)
    assert _alert_local_time(source).tzname() == source.astimezone().tzname()


def test_system_timezone_preserves_source_date_dst(monkeypatch):
    if not hasattr(time, "tzset"):
        pytest.skip("System timezone control unavailable")
    original = os.environ.get("TZ")
    try:
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        time.tzset()
        monkeypatch.delenv("TZ")
        winter = datetime(2026, 1, 11, 2, 37, 15, tzinfo=timezone.utc)
        summer = datetime(2026, 7, 11, 2, 37, 15, tzinfo=timezone.utc)
        assert _alert_local_time(winter).utcoffset() == winter.astimezone(ZoneInfo("America/Los_Angeles")).utcoffset()
        assert _alert_local_time(summer).utcoffset() == summer.astimezone(ZoneInfo("America/Los_Angeles")).utcoffset()
    finally:
        if original is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", original)
        time.tzset()


def test_unavailable_system_timezone_falls_back_to_utc():
    class UnavailableLocalTime(datetime):
        def astimezone(self, tz=None):
            if tz is None:
                raise OSError("System timezone unavailable")
            return super().astimezone(tz)

    source = UnavailableLocalTime(2026, 1, 11, 2, 37, 15, tzinfo=timezone.utc)
    assert _alert_local_time(source).tzname() == "UTC"
