"""Regression coverage for transient database failures in scheduled checks."""
from __future__ import annotations

from psycopg import OperationalError

import scheduler
from scheduler import MonitoringScheduler
from schemas import HealthReport


class DatabaseWithOneTransientFailure:
    available = True

    def __init__(self):
        self.check_calls = 0
        self.applied = []

    def next_check_number(self):
        self.check_calls += 1
        if self.check_calls == 1:
            raise OperationalError("SSL error: unexpected eof while reading")
        return 1

    def load_tracked_findings(self):
        return {}

    def apply_diff(self, diff, now):
        self.applied.append(diff)


class DisabledNotifier:
    enabled = False


def test_scheduled_check_retries_one_transient_postgres_failure(monkeypatch):
    report = HealthReport(
        overall_severity="ok",
        summary="Cluster healthy.",
        findings=[],
    )
    monkeypatch.setattr(scheduler, "run_structured_health_check", lambda: (report, {"unhealthy_pods": []}))
    database = DatabaseWithOneTransientFailure()

    MonitoringScheduler(None, DisabledNotifier(), db=database)._do_check("sched-test")

    assert database.check_calls == 2
    assert len(database.applied) == 1
