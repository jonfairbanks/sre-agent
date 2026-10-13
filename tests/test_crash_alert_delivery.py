"""Crash-alert queue and delivery contracts. Postgres tests need an isolated DB."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import uuid
from unittest.mock import Mock

import pytest

from notification_delivery import NotificationDelivery
from persistence import NullDatabase, PostgresDatabase
from slack_health import SlackDeliveryError
from slack_notifier import SlackNotifier

NOW = datetime.now(timezone.utc)
PAYLOAD = {"severity": "critical", "title": "Pod Is Crashing", "message": "Container laya is in CrashLoopBackOff.", "namespace": "decider"}


@pytest.fixture
def db():
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    from psycopg import connect, sql
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    schema = "crash_alert_test_" + uuid.uuid4().hex
    with connect(dsn) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    pool = ConnectionPool(dsn, kwargs={"row_factory": dict_row, "options": f"-csearch_path={schema}"}, min_size=1, max_size=4, open=True)
    database = PostgresDatabase(pool)
    try:
        database.setup()
        yield database
    finally:
        database.close()
        # Only this test's newly created, uniquely named schema is removed.
        with connect(dsn) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_null_database_requires_watcher_fallback():
    assert NullDatabase().enqueue_crash_alert("uid/laya", PAYLOAD) is False


def test_concurrent_streams_and_restart_enqueue_only_once(db):
    other = PostgresDatabase(db._pool)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda n: (db if n % 2 else other).enqueue_crash_alert("uid/laya", PAYLOAD), range(8)))
    assert sum(results) == 1
    assert other.enqueue_crash_alert("uid/laya", PAYLOAD) is False
    assert db.enqueue_crash_alert("uid/sidecar", PAYLOAD)
    assert db.enqueue_crash_alert("replacement-uid/laya", PAYLOAD)
    rows = db.claim_pending_notifications(datetime.now(timezone.utc), limit=10)
    assert len(rows) == 3
    assert all(row["payload"]["type"] == "crash_alert" for row in rows)
    assert db.load_tracked_findings() == {}


def test_failed_outbox_insert_does_not_reserve_identity(db):
    with pytest.raises(Exception):
        db.enqueue_crash_alert("uid/laya", {**PAYLOAD, "message": object()})
    with db._pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM crash_alert_dedup").fetchone()["count"] == 0
        assert conn.execute("SELECT COUNT(*) AS count FROM notification_outbox").fetchone()["count"] == 0
    assert db.enqueue_crash_alert("uid/laya", PAYLOAD)


def test_retention_removes_old_delivered_identity_but_keeps_pending(db):
    assert db.enqueue_crash_alert("old-delivered/laya", PAYLOAD)
    assert db.enqueue_crash_alert("old-pending/laya", PAYLOAD)
    old = datetime.now(timezone.utc) - timedelta(days=31)
    with db._pool.connection() as conn:
        conn.execute("UPDATE crash_alert_dedup SET created_at=%s", (old,))
        conn.execute("""UPDATE notification_outbox SET status='delivered', delivered_at=%s
                        WHERE id=(SELECT notification_id FROM crash_alert_dedup WHERE alert_key='old-delivered/laya')""", (old,))
    assert db.enqueue_crash_alert("new/laya", PAYLOAD)
    with db._pool.connection() as conn:
        keys = {row["alert_key"] for row in conn.execute("SELECT alert_key FROM crash_alert_dedup").fetchall()}
    assert keys == {"old-pending/laya", "new/laya"}
    assert not db.enqueue_crash_alert("old-pending/laya", PAYLOAD)


class Queue:
    def __init__(self):
        self.row = {"id": "d4c7ba56-920f-47da-a91f-30c28af5b6e3", "attempts": 0, "payload": {**PAYLOAD, "type": "crash_alert"}}
        self.due = NOW
        self.delivered = False

    def claim_pending_notifications(self, now, limit):
        if self.delivered or now < self.due:
            return []
        self.row["attempts"] += 1
        self.due = now + timedelta(minutes=2)
        return [dict(self.row)]

    def mark_notification_delivered(self, notification_id, message_ts, now, *, attempts):
        assert attempts == self.row["attempts"]
        self.delivered = True
        return True

    def reschedule_notification(self, notification_id, error_type, now, retry_after_seconds, *, attempts):
        self.error_type = error_type
        self.due = now + timedelta(seconds=retry_after_seconds)


def test_crash_payload_retries_after_restart_with_stable_message_id():
    db = Queue()
    notifier = Mock(enabled=True)
    notifier.send_alert.side_effect = [SlackDeliveryError("RateLimited", 120), "1.2"]
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 0
    assert db.due == NOW + timedelta(seconds=120)
    assert NotificationDelivery(db, notifier, clock=lambda: db.due).drain_once() == 1
    assert NotificationDelivery(db, notifier, clock=lambda: db.due).drain_once() == 0
    notifier.send_structured_report.assert_not_called()
    assert notifier.send_alert.call_count == 2
    assert all(call.kwargs["notification_id"] == db.row["id"] and call.kwargs["raise_on_error"] for call in notifier.send_alert.call_args_list)


def test_missing_crash_message_timestamp_retries():
    db = Queue()
    notifier = Mock(enabled=True)
    notifier.send_alert.return_value = None
    assert NotificationDelivery(db, notifier, clock=lambda: NOW).drain_once() == 0
    assert db.error_type == "NoMessageTimestamp"
    assert not db.delivered


def test_wake_from_watcher_thread_drains_without_waiting_for_interval():
    async def run():
        db = Queue()
        db.delivered = True
        notifier = Mock(enabled=True)
        notifier.send_alert.return_value = "1.2"
        delivery = NotificationDelivery(db, notifier, clock=lambda: NOW, retry_interval_seconds=60)
        await delivery.start()
        await asyncio.sleep(.02)
        db.delivered = False
        await asyncio.to_thread(delivery.wake)
        for _ in range(100):
            if db.delivered:
                break
            await asyncio.sleep(.005)
        assert db.delivered
        await asyncio.wait_for(delivery.stop(), timeout=1)
    asyncio.run(run())


def test_alert_forwards_durable_delivery_options_and_surfaces_failure():
    notifier = SlackNotifier(bot_token="xoxb-test", channel="#test")
    notifier._client = Mock()
    notifier._client.chat_postMessage.return_value = {"ts": "1.2", "channel": "C1"}
    assert notifier.send_alert(**PAYLOAD, notification_id="notification-one", raise_on_error=True) == "1.2"
    assert notifier._client.chat_postMessage.call_args.kwargs["client_msg_id"] == "notification-one"
    notifier._client.chat_postMessage.side_effect = BrokenPipeError("private error")
    with pytest.raises(SlackDeliveryError):
        notifier.send_alert(**PAYLOAD, notification_id="notification-one", raise_on_error=True)
    assert notifier.send_alert(**PAYLOAD) is None


def test_delivery_drains_full_batches_without_retry_pause():
    db, notifier = Mock(), Mock(enabled=True)
    delivery = NotificationDelivery(db, notifier, retry_interval_seconds=3600)
    calls = []
    def drain():
        calls.append(1)
        if len(calls) == 2:
            delivery._stop.set()
            delivery._wake.set()
            return 0
        return 10
    delivery.drain_once = drain
    async def run():
        await asyncio.wait_for(delivery._run(), timeout=1)
    asyncio.run(run())
    assert len(calls) == 2
