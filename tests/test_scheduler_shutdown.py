"""Executor checks finish before shutdown can close their database."""
import asyncio
import threading
from unittest.mock import Mock

import pytest

import scheduler
from scheduler import MonitoringScheduler
from schemas import HealthReport


def gated_scheduler(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    operations = []
    database = Mock(available=True)
    database.closed = False
    database.next_check_number.return_value = 1
    database.load_tracked_findings.return_value = {}
    database.load_finding_aliases.return_value = {}

    def collect():
        entered.set()
        assert release.wait(5), "Test did not release the check thread"
        return HealthReport(overall_severity="ok", summary="Healthy"), {"unhealthy_pods": []}

    def persist(*args):
        assert not database.closed, "Database closed before the check persisted"
        operations.append("persist")

    def close():
        database.closed = True
        operations.append("close")

    database.record_monitor_check.side_effect = persist
    database.close.side_effect = close
    notifier = Mock(enabled=True)
    monkeypatch.setattr(scheduler, "run_structured_health_check", collect)
    monkeypatch.setattr(scheduler, "_monitor_status_changed", lambda *_: False)
    monkeypatch.setattr("health_evidence.reconcile_report", lambda report, *_: report)
    monitor = MonitoringScheduler(None, notifier, db=database)
    return monitor, database, notifier, entered, release, operations


async def wait_for_thread(event):
    assert await asyncio.to_thread(event.wait, 1), "Check thread did not start"


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_scheduled_check_persists_before_database_close(monkeypatch):
    monitor, database, notifier, entered, release, operations = gated_scheduler(monkeypatch)

    async def run():
        async def scheduled_loop():
            await monitor._run_check()
        monkeypatch.setattr(monitor, "_loop", scheduled_loop)
        await monitor.start()
        await wait_for_thread(entered)

        async def shutdown():
            await monitor.stop()
            database.close()

        stopped = asyncio.create_task(shutdown())
        try:
            await settle()
            assert not stopped.done()
            assert not database.closed
            assert monitor._task.cancelled()
        finally:
            release.set()
        await asyncio.wait_for(stopped, 1)
        assert operations == ["persist", "close"]
        notifier.send_alert.assert_not_called()
    asyncio.run(run())


def test_canceled_manual_check_still_drains_before_close(monkeypatch):
    monitor, database, notifier, entered, release, operations = gated_scheduler(monkeypatch)

    async def run():
        # Manual checks remain usable when the background scheduler is disabled.
        request = asyncio.create_task(monitor.trigger_now())
        await wait_for_thread(entered)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        stopped = asyncio.create_task(monitor.stop())
        try:
            await settle()
            assert not stopped.done()
            assert len(monitor._inflight_checks) == 1
            with pytest.raises(RuntimeError, match="stopping"):
                await monitor.trigger_now()
        finally:
            release.set()
        await asyncio.wait_for(stopped, 1)
        database.close()
        assert operations == ["persist", "close"]
        notifier.send_alert.assert_not_called()
    asyncio.run(run())


def test_canceled_shutdown_preserves_drain_and_cannot_close_database(monkeypatch):
    monitor, database, notifier, entered, release, operations = gated_scheduler(monkeypatch)

    async def run():
        request = asyncio.create_task(monitor.trigger_now())
        await wait_for_thread(entered)

        async def shutdown():
            await monitor.stop()
            database.close()

        stopped = asyncio.create_task(shutdown())
        await settle()
        stopped.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await stopped
            assert not monitor._stop_task.done()
            assert not database.closed
        finally:
            release.set()
        await asyncio.wait_for(request, 1)
        await asyncio.wait_for(shutdown(), 1)
        assert operations == ["persist", "close"]
        notifier.send_alert.assert_not_called()
    asyncio.run(run())


def test_stop_drains_all_manual_checks_including_queued_workers(monkeypatch):
    monitor, database, notifier, entered, release, operations = gated_scheduler(monkeypatch)

    async def run():
        first = asyncio.create_task(monitor.trigger_now())
        await wait_for_thread(entered)
        second = asyncio.create_task(monitor.trigger_now())
        await settle()
        stopped = asyncio.create_task(monitor.stop())
        try:
            await settle()
            assert len(monitor._inflight_checks) == 2
            assert not stopped.done()
        finally:
            release.set()
        await asyncio.wait_for(asyncio.gather(first, second, stopped), 1)
        database.close()
        assert operations == ["persist", "persist", "close"]
        notifier.send_alert.assert_not_called()
    asyncio.run(run())


def test_idle_scheduler_stop_is_prompt_and_restart_is_usable(monkeypatch):
    async def run():
        monitor = MonitoringScheduler(None, Mock(enabled=False))
        sleeping = asyncio.Event()

        async def idle_loop():
            sleeping.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(monitor, "_loop", idle_loop)
        await monitor.start()
        await sleeping.wait()
        await asyncio.wait_for(monitor.stop(), .5)
        assert monitor._task.cancelled()
        await monitor.stop()
        await monitor.start()
        assert not monitor._stopping
        await asyncio.wait_for(monitor.stop(), .5)
    asyncio.run(run())
