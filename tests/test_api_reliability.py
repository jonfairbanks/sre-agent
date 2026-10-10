"""API integration for durable Slack reports and bounded transport status."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import threading

from fastapi.testclient import TestClient
import pytest
import api
from slack_health import SlackRuntimeHealth
from schemas import HealthReport


@pytest.fixture
def api_state(monkeypatch):
    for name in ("_notifier", "_agent", "_scheduler", "_db", "_delivery", "_slack_handler"):
        monkeypatch.setattr(api, name, None)
    monkeypatch.setattr(api, "_slack_health", SlackRuntimeHealth())
    monkeypatch.setattr(api, "_slack_shutdown", threading.Event())


def test_liveness_skips_storage_and_metrics_read_bounded_status(api_state, monkeypatch):
    db = Mock(available=True, kind="postgres")
    monkeypatch.setattr(api, "_db", db)
    api._slack_health.socket_state("disconnected")
    api._slack_health.outbound_result()
    client = TestClient(api.app)
    response = client.get("/health")
    assert response.json()["status"] == "ok"
    assert response.json()["slack"]["socket"]["connected"] is False
    assert response.json()["slack"]["outbound"]["state"] == "healthy"
    db.delivery_status.assert_not_called()
    db.recent_monitor_checks.assert_not_called()
    db.delivery_status.return_value = {"available": True, "pending": 0, "in_flight": 0}
    db.recent_monitor_checks.return_value = []
    metrics = client.get("/metrics")
    assert "sre_agent_slack_socket_connected 0" in metrics.text
    assert "sre_agent_slack_outbound_successes_total 1" in metrics.text
    assert "{" not in metrics.text
    db.delivery_status.assert_called_once()
    db.recent_monitor_checks.assert_called_once()


def test_operational_status_and_ignored_findings(api_state, monkeypatch):
    db = Mock(available=True, kind="postgres")
    db.delivery_status.return_value = {"available": True, "pending": 2, "in_flight": 1}
    db.recent_monitor_checks.return_value = [{"observed_at": datetime.now(timezone.utc),
        "report": {"overall_severity": "unknown", "analysis_valid": False,
                   "coverage": [{"area": "pods", "status": "unavailable"}]}}]
    db.list_muted_findings.return_value = [{"title": "Pod Not Ready", "ignored_forever": True}]
    monkeypatch.setattr(api, "_db", db)
    client = TestClient(api.app)
    status = client.get("/api/status").json()
    assert status["delivery"]["pending"] == 2
    assert status["recent_monitor_check"]["overall_severity"] == "unknown"
    assert client.get("/api/findings/ignored").json()["count"] == 1
    assert client.get("/api/findings/ignored?limit=101").status_code == 422


def test_status_failure_exposes_safe_error_type(api_state, monkeypatch, caplog):
    db = Mock(available=True, kind="postgres")
    db.delivery_status.side_effect = RuntimeError("postgres://credential-token")
    monkeypatch.setattr(api, "_db", db)
    status = TestClient(api.app).get("/api/status").json()
    assert status["status"] == "ok"
    assert status["delivery"] == {"available": False, "error_type": "RuntimeError"}
    assert "credential" not in caplog.text
    metrics = TestClient(api.app).get("/metrics").text
    assert "sre_agent_database_read_healthy 0" in metrics
    assert "sre_agent_notifications_pending" not in metrics
    assert "sre_agent_monitor_last_check_timestamp_seconds" not in metrics


@pytest.mark.parametrize("durable", [False, True])
def test_startup_injects_history_tools_and_manages_delivery(api_state, monkeypatch, durable):
    import llm
    import persistence
    import scheduler
    import slack_notifier
    import notification_delivery

    monkeypatch.setattr(llm, "validate_provider_credentials", lambda: None)
    db = Mock(available=durable, kind="postgres" if durable else "memory")
    monkeypatch.setattr(persistence, "init_persistence", lambda url: ("checkpoint", "store", db))
    notifier = Mock(enabled=False, health=SlackRuntimeHealth())
    monkeypatch.setattr(slack_notifier, "make_notifier", lambda: notifier)
    created = {}
    def create_agent(**kwargs):
        created.update(kwargs)
        return Mock()
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(create_sre_agent=create_agent))
    scheduled = Mock(_running=False)
    async def start(): pass
    async def stop(): pass
    scheduled.start = start
    scheduled.stop = stop
    make_scheduler = Mock(return_value=scheduled)
    monkeypatch.setattr(scheduler, "MonitoringScheduler", make_scheduler)
    deliveries = []
    class Delivery:
        def __init__(self, passed_db, passed_notifier):
            assert passed_db is db and passed_notifier is notifier
            self.started = self.stopped = False
            deliveries.append(self)
        async def start(self): self.started = True
        async def stop(self): self.stopped = True
    monkeypatch.setattr(notification_delivery, "NotificationDelivery", Delivery)
    monkeypatch.setattr(api, "threading", SimpleNamespace(Thread=Mock(return_value=Mock())))
    async def run():
        async with api.lifespan(api.app):
            names = {tool.name for tool in created["extra_tools"]}
            assert {"get_incident_history", "get_finding_history", "incident_timeline", "list_ignored_findings"} <= names
            assert make_scheduler.call_args.kwargs["delivery"] is (deliveries[0] if durable else None)
            if durable:
                assert deliveries[0].started
        if durable:
            assert deliveries[0].stopped
        db.close.assert_called_once()
        assert api._slack_health.status()["socket"]["state"] == "stopped"
    asyncio.run(run())


def test_manual_slack_check_reconciles_then_persists_and_queues(api_state, monkeypatch):
    import scheduler
    import health_evidence
    report = HealthReport(overall_severity="unknown", summary="Collection unavailable")
    data = {"coverage": [], "recovered_runtime_probe_events": []}
    monkeypatch.setattr(scheduler, "run_structured_health_check", lambda: (report, data))
    stored = {"prior": object()}
    reconciled = Mock(return_value=report)
    monkeypatch.setattr(health_evidence, "reconcile_report", reconciled)
    db = Mock(available=True)
    db.load_tracked_findings.return_value = {}  # typed diff tests cover tracked rows
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_scheduler", SimpleNamespace(_check_lock=threading.Lock()))
    notifier = Mock(enabled=True)
    delivery = Mock()
    monkeypatch.setattr(api, "_notifier", notifier)
    monkeypatch.setattr(api, "_delivery", delivery)
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "1.2"}
    session = api.Session("slack-1", "slack-1", source="slack")
    api._run_structured_health_check_for_slack("health audit", session, client, "C1", "1.0")
    reconciled.assert_called_once_with(report, data, {})
    args = db.record_monitor_check.call_args.args
    assert args[0].startswith("slack-1-check-")
    assert args[1] == 0
    assert args[6]["channel"] == "C1"
    assert args[6]["thread_ts"] == "1.0"
    assert args[6]["source"] == "slack"
    assert args[6]["diff"] is not None
    db.next_check_number.assert_not_called()
    delivery.drain_once.assert_called_once()
    notifier.send_structured_report.assert_not_called()
    assert session.status == api.SessionStatus.DONE


def test_socket_startup_installs_safe_health_hooks(api_state, monkeypatch, caplog):
    import slack_bolt
    import slack_bolt.adapter.socket_mode
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    class Bolt:
        def __init__(self, **kwargs): self.actions = {}
        def event(self, name): return lambda fn: fn
        def action(self, name):
            def register(fn):
                self.actions[name] = fn
                return fn
            return register
    class Handler:
        def __init__(self, bolt, token, **kwargs):
            assert "sre_ignore_finding" in bolt.actions
            self.client = SimpleNamespace(on_message_listeners=[], on_error_listeners=[],
                on_close_listeners=[], is_connected=lambda: False)
        def connect(self): raise BrokenPipeError("xapp-private-token")
    monkeypatch.setattr(slack_bolt, "App", Bolt)
    monkeypatch.setattr(slack_bolt.adapter.socket_mode, "SocketModeHandler", Handler)
    api._start_slack_bolt(None)
    status = api._slack_health.status()["socket"]
    assert status["state"] == "disconnected"
    assert status["last_error_type"] == "BrokenPipeError"
    assert len(api._slack_handler.client.on_error_listeners) == 1
    assert "private-token" not in caplog.text


def test_metrics_include_queue_age_and_stale_last_check(api_state, monkeypatch):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    db = Mock(available=True, kind="postgres")
    db.delivery_status.return_value = {"available": True, "pending": 2, "in_flight": 1,
        "oldest_pending_at": now - timedelta(seconds=90), "last_delivered_at": now - timedelta(hours=2)}
    db.recent_monitor_checks.return_value = [{"observed_at": now - timedelta(days=2),
        "report": {"overall_severity": "unknown", "analysis_valid": False}}]
    monkeypatch.setattr(api, "_db", db)
    text = TestClient(api.app).get("/metrics").text
    assert "sre_agent_notifications_pending 2" in text
    assert "sre_agent_notifications_in_flight 1" in text
    assert "sre_agent_notification_oldest_pending_age_seconds" in text
    assert "sre_agent_notification_last_delivered_timestamp_seconds" in text
    assert "sre_agent_monitor_last_analysis_valid 0" in text
    assert "sre_agent_monitor_last_check_timestamp_seconds" in text
    since = db.recent_monitor_checks.call_args.kwargs["since"]
    assert (now - since).days >= 29
