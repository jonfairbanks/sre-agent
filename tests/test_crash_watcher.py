"""Immediate crash evidence, deduplication, and watch recovery contracts."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes import client as k
from kubernetes.client.exceptions import ApiException

from crash_watcher import CrashWatcher, collect_crash_evidence, pod_crashes, safe_evidence

NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)


def container(name="app", *, waiting=None, reason=None, exit_code=1,
              ready=False, age_seconds=0, last=False):
    termination = (k.V1ContainerStateTerminated(
        exit_code=exit_code, reason=reason, finished_at=NOW - timedelta(seconds=age_seconds))
        if reason else None)
    return k.V1ContainerStatus(
        name=name, image="test", image_id="test", ready=ready, restart_count=2,
        state=k.V1ContainerState(
            waiting=k.V1ContainerStateWaiting(reason=waiting) if waiting else None,
            terminated=None if last else termination),
        last_state=k.V1ContainerState(terminated=termination if last else None))


def pod(*, main=None, init=None, uid="pod-uid", version="11", phase="Running"):
    return k.V1Pod(
        metadata=k.V1ObjectMeta(name="worker-1", namespace="apps", uid=uid,
                               resource_version=version, creation_timestamp=NOW),
        status=k.V1PodStatus(phase=phase, container_statuses=main or [],
                            init_container_statuses=init or []))


def event(*, uid="pod-uid", name="app", age_seconds=0, reason="BackOff",
          message=None, field_path=True):
    return k.CoreV1Event(
        metadata=k.V1ObjectMeta(namespace="apps", resource_version="12"),
        involved_object=k.V1ObjectReference(
            kind="Pod", name="worker-1", uid=uid,
            field_path=f"spec.containers{{{name}}}" if field_path else None),
        type="Warning", reason=reason,
        message=message or f"Back-off restarting failed container {name} in pod worker-1",
        last_timestamp=NOW - timedelta(seconds=age_seconds))


def watcher(*, durable=False, client=None, factory=None, evidence=None):
    db = NS(available=durable, enqueue_crash_alert=Mock(return_value=True))
    notifier = NS(enabled=True, send_alert=Mock(return_value="slack-ts"))
    delivery = NS(wake=Mock())
    instance = CrashWatcher(db, notifier, delivery, clock=lambda: NOW,
                            client_factory=lambda: client, watch_factory=factory,
                            evidence_collector=evidence or Mock(return_value=""))
    return instance


@pytest.mark.parametrize("init", [False, True])
@pytest.mark.parametrize("status", [
    {"waiting": "CrashLoopBackOff"}, {"reason": "OOMKilled"},
    {"reason": "Error"}, {"reason": "ContainerCannotRun"},
    {"reason": "Other", "exit_code": 42}, {"reason": "Error", "last": True},
])
def test_init_and_main_crash_evidence_alerts_immediately(init, status):
    instance = watcher()
    crashing = container(**status)
    instance.handle_pod(pod(init=[crashing]) if init else pod(main=[crashing]))
    instance.notifier.send_alert.assert_called_once()
    args = instance.notifier.send_alert.call_args
    assert args.args[0:2] == ("critical", "Pod Crash Detected")
    assert "`worker-1`" in args.args[2] and "`app`" in args.args[2]
    assert args.kwargs == {"namespace": "apps"}


@pytest.mark.parametrize("status", [
    {"ready": True}, {"ready": True, "reason": "Error", "last": True},
    {"reason": "Completed", "exit_code": 0},
    {"reason": "Completed", "exit_code": 0, "last": True},
    {"reason": "Error", "age_seconds": 301}, {"waiting": "ImagePullBackOff"},
])
def test_healthy_clean_old_and_image_pull_states_are_ignored(status):
    assert pod_crashes(pod(main=[container(**status)]), NOW) == []


def test_succeeded_and_deleting_pods_are_ignored():
    crashing = pod(main=[container(waiting="CrashLoopBackOff")], phase="Succeeded")
    assert pod_crashes(crashing, NOW) == []
    crashing.status.phase = "Running"
    crashing.metadata.deletion_timestamp = NOW
    assert pod_crashes(crashing, NOW) == []


@pytest.mark.parametrize("first", ["pod", "event"])
def test_pod_and_event_deduplicate_by_uid_and_container(first):
    instance = watcher()
    observations = {"pod": lambda: instance.handle_pod(pod(main=[container(waiting="CrashLoopBackOff")])),
                    "event": lambda: instance.handle_event(event())}
    observations[first]()
    observations["event" if first == "pod" else "pod"]()
    observations[first]()
    assert instance.notifier.send_alert.call_count == 1
    instance.handle_event(event(uid="replacement-pod"))
    instance.handle_event(event(name="sidecar"))
    assert instance.notifier.send_alert.call_count == 3


def test_durable_delivery_uses_same_idempotency_key_and_wakes_on_duplicates():
    instance = watcher(durable=True)
    instance.handle_pod(pod(main=[container(waiting="CrashLoopBackOff")]))
    instance.db.enqueue_crash_alert.return_value = False
    instance.handle_event(event())
    assert [call.args[0] for call in instance.db.enqueue_crash_alert.call_args_list] == [
        "pod-uid/app", "pod-uid/app"]
    assert instance.delivery.wake.call_count == 2
    instance.notifier.send_alert.assert_not_called()


def test_deleted_pod_backoff_event_alerts_with_injected_evidence():
    instance = watcher(client=Mock())
    instance.handle_event(event(uid="already-deleted", field_path=False))
    instance.notifier.send_alert.assert_called_once()
    assert "CrashLoopBackOff" in instance.notifier.send_alert.call_args.args[2]
    instance.client_factory().assert_not_called()


@pytest.mark.parametrize("changes", [
    {"age_seconds": 301}, {"reason": "Failed"},
    {"message": "Back-off pulling image registry.example/app"},
    {"uid": None},
])
def test_stale_image_pull_and_unidentified_events_are_ignored(changes):
    instance = watcher()
    instance.handle_event(event(**changes))
    instance.notifier.send_alert.assert_not_called()


def test_fresh_series_observation_is_used_for_aggregated_event():
    instance = watcher()
    aggregated = event(age_seconds=600)
    aggregated.series = k.CoreV1EventSeries(count=8, last_observed_time=NOW)
    instance.handle_event(aggregated)
    instance.notifier.send_alert.assert_called_once()


def test_failed_memory_delivery_remains_pending_for_retry():
    instance = watcher()
    instance.notifier.send_alert.side_effect = [RuntimeError("unavailable"), None, "ts"]
    instance.handle_event(event())
    assert "pod-uid/app" in instance._pending and instance._seen == {}
    with instance._lock:
        instance._retry_memory()
        assert "pod-uid/app" in instance._pending
        instance._retry_memory()
    assert instance._pending == {} and "pod-uid/app" in instance._seen


class StopFlag:
    """Skip retry delays while preserving the consumer's stop contract."""
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stopped


def consume_fixture(scripts, pages=None, kind="pods"):
    listing = Mock(side_effect=pages or [NS(items=[], metadata=NS(resource_version="10"))])
    api_client = NS(close=Mock())
    client = NS(list_pod_for_all_namespaces=listing, list_event_for_all_namespaces=listing,
                api_client=api_client)
    calls, streams = [], []
    instance = watcher(client=client)
    instance._stop = StopFlag()

    def factory():
        script = scripts[len(streams)]
        stream = NS(stop=Mock())
        streams.append(stream)

        def iterate(method, **kwargs):
            calls.append(kwargs)
            for change in script:
                if isinstance(change, Exception):
                    raise change
                yield change
            if len(streams) == len(scripts):
                instance._stop.set()
        stream.stream = iterate
        return stream

    instance.watch_factory = factory
    return instance, listing, calls, streams, api_client


def change(version, kind="MODIFIED"):
    return {"type": kind, "object": pod(version=version)}


@pytest.mark.parametrize("kind", ["pods", "events"])
def test_watch_timeout_reconnects_from_last_resource_version(kind):
    instance, listing, calls, streams, api_client = consume_fixture(
        [[change("11", "BOOKMARK")], [change("12", "BOOKMARK")]], kind=kind)
    instance._consume(kind)
    assert listing.call_count == 1
    assert [call["resource_version"] for call in calls] == ["10", "11"]
    assert calls[0]["timeout_seconds"] == 20
    if kind == "events":
        assert listing.call_args.kwargs["field_selector"] == "type=Warning"
        assert calls[0]["field_selector"] == "type=Warning"
    assert all(stream.stop.call_count == 1 for stream in streams)
    api_client.close.assert_called_once()
    assert not instance._connected[kind]


@pytest.mark.parametrize("as_watch_error", [False, True])
def test_expired_cursor_relists_after_410(as_watch_error):
    expired = ({"type": "ERROR", "object": NS(code=410)} if as_watch_error
               else ApiException(status=410))
    pages = [NS(items=[], metadata=NS(resource_version="10")),
             NS(items=[], metadata=NS(resource_version="20"))]
    instance, listing, calls, _, _ = consume_fixture([[expired], []], pages)
    instance._consume("pods")
    assert listing.call_count == 2
    assert [call["resource_version"] for call in calls] == ["10", "20"]


def test_callback_failure_does_not_advance_cursor():
    instance, listing, calls, _, _ = consume_fixture([[change("11")], [change("11")]])
    instance.handle_pod = Mock(side_effect=[RuntimeError("database unavailable"), None])
    instance._consume("pods")
    assert listing.call_count == 1
    assert [call["resource_version"] for call in calls] == ["10", "10"]
    assert instance.handle_pod.call_count == 2


def test_deleted_updates_advance_cursor_without_alerting():
    instance, _, calls, _, _ = consume_fixture([[change("11", "DELETED")], []])
    instance.handle_pod = Mock()
    instance._consume("pods")
    instance.handle_pod.assert_not_called()
    assert calls[1]["resource_version"] == "11"


def test_shutdown_joins_workers_and_start_is_idempotent():
    instance = watcher(durable=True)
    instance._consume = lambda kind: instance._stop.wait(2)

    async def lifecycle():
        await instance.start()
        workers = list(instance._threads)
        assert len(workers) == 2 and instance.status()["running"]
        await instance.start()
        assert instance._threads == workers
        await instance.stop()
        assert not any(worker.is_alive() for worker in workers)
        assert instance._threads == [] and not instance.status()["running"]
    asyncio.run(lifecycle())


def test_memory_retry_stops_before_sending_backlog():
    notifier = Mock(enabled=True)
    watcher = CrashWatcher(Mock(available=False), notifier)
    watcher._pending["uid/main"] = {"severity": "critical", "title": "Pod Crash Detected",
                                     "message": "CrashLoopBackOff", "namespace": "laya"}
    watcher._stop.set()
    watcher._retry_memory()
    notifier.send_alert.assert_not_called()
    assert "uid/main" in watcher._pending



def evidence_client(crashing=None, logs="failure line"):
    return NS(read_namespaced_pod=Mock(return_value=crashing or pod(main=[container(reason="Error")])),
              read_namespaced_pod_log=Mock(return_value=logs),
              list_namespaced_event=Mock(return_value=NS(items=[])),
              api_client=NS(close=Mock()))


def collect(client, **kwargs):
    return collect_crash_evidence(lambda: client, uid="pod-uid", namespace="apps",
                                  pod_name="worker-1", container="app", now=NOW, **kwargs)


@pytest.mark.parametrize("init", [False, True])
@pytest.mark.parametrize("last", [False, True])
def test_evidence_reads_exact_main_or_init_container_logs(init, last):
    cs = container(name="init-worker" if init else "app", reason="Error", last=last)
    cs.restart_count = 0 if not last else 2
    snapshot = pod(init=[cs]) if init else pod(main=[cs])
    client = evidence_client(snapshot)
    result = collect_crash_evidence(lambda: client, uid="pod-uid", namespace="apps",
        pod_name="worker-1", container=cs.name, now=NOW, observed_pod=snapshot)
    assert "Termination: Error (exit 1)" in result
    assert "\n```\nfailure line\n```" in result
    assert client.read_namespaced_pod_log.call_args.kwargs == {
        "container": cs.name, "previous": last, "tail_lines": 6,
        "limit_bytes": 1400, "_request_timeout": (1, 1)}
    assert client.read_namespaced_pod.call_args.kwargs["_request_timeout"] == (1, 1)
    assert client.list_namespaced_event.call_args.kwargs == {
        "field_selector": "involvedObject.uid=pod-uid,type=Warning", "limit": 10,
        "_request_timeout": (1, 1)}
    client.api_client.close.assert_called_once()


def test_log_code_block_cannot_be_closed_by_container_output():
    result = collect(evidence_client(logs="first line\n```<!channel>\nlast line"))
    assert result.count("```") == 2
    assert "&lt;!channel&gt;" in result
    assert result.endswith("last line\n```")


def test_logs_use_latest_status_when_observed_termination_has_restarted():
    client = evidence_client(pod(main=[container(waiting="CrashLoopBackOff", reason="Error", last=True)]))
    result = collect(client, observed_pod=pod(main=[container(reason="Error", exit_code=42)]))
    assert client.read_namespaced_pod_log.call_args.kwargs["previous"] is True
    assert "exit 42" in result


@pytest.mark.parametrize("error", [ApiException(status=404, reason="private body"),
                                    ApiException(status=403, reason="private body"),
                                    TimeoutError("private body")])
@pytest.mark.parametrize("method", ["read_namespaced_pod", "read_namespaced_pod_log", "list_namespaced_event"])
def test_evidence_api_failures_do_not_prevent_alerts_or_leak_exception_bodies(error, method):
    client = evidence_client()
    getattr(client, method).side_effect = error
    instance = watcher(evidence=lambda **kwargs: collect_crash_evidence(lambda: client, **kwargs))
    instance.handle_pod(pod(main=[container(reason="Error", exit_code=42)]))
    instance.notifier.send_alert.assert_called_once()
    message = instance.notifier.send_alert.call_args.args[2]
    assert "exit 42" in message and "private body" not in message
    if method == "read_namespaced_pod":
        client.read_namespaced_pod_log.assert_not_called()
    if method != "list_namespaced_event":
        assert "Recent Logs: unavailable" in message
    else:
        assert "Recent Warning Events: unavailable" in message


def test_replacement_uid_is_never_used_for_logs():
    client = evidence_client(pod(uid="replacement"))
    result = collect(client)
    client.read_namespaced_pod_log.assert_not_called()
    assert "Recent Logs: unavailable" in result


def test_deleted_pod_event_keeps_context_if_lookup_and_listing_fail():
    client = evidence_client()
    client.read_namespaced_pod.side_effect = ApiException(status=404)
    client.list_namespaced_event.side_effect = ApiException(status=403)
    instance = watcher(evidence=lambda **kwargs: collect_crash_evidence(lambda: client, **kwargs))
    instance.handle_event(event())
    message = instance.notifier.send_alert.call_args.args[2]
    assert "Recent Logs: unavailable" in message
    assert "BackOff: Back-off restarting failed container app" in message
    client.read_namespaced_pod_log.assert_not_called()


def test_event_context_is_recent_same_uid_warning_deduplicated_and_bounded():
    client = evidence_client()
    wrong_type = event(message="normal hidden")
    wrong_type.type = "Normal"
    client.list_namespaced_event.return_value.items = [
        event(uid="different", message="wrong uid hidden"), event(age_seconds=301, message="old hidden"),
        wrong_type, event(message="newest", age_seconds=0), event(message="newest", age_seconds=0),
        event(message="second", age_seconds=1), event(message="third", age_seconds=2),
        event(message="fourth hidden", age_seconds=3)]
    result = collect(client)
    assert "newest" in result and "second" in result and "third" in result
    assert result.count("newest") == 1 and "hidden" not in result


def test_log_lines_and_message_size_are_bounded():
    client = evidence_client(logs="\n".join(f"line-{n} " + "a" * 400 for n in range(12)))
    instance = watcher(evidence=lambda **kwargs: collect_crash_evidence(lambda: client, **kwargs))
    instance.handle_pod(pod(main=[container(reason="Error")]))
    message = instance.notifier.send_alert.call_args.args[2]
    assert "line-5" not in message and "line-6" in message and len(message) < 2500


@pytest.mark.parametrize("secret", [
    '"token": "sensitive-token"', '"password":"sensitive-password"',
    "API_KEY=sensitive-key", '"api-key": "sensitive-api-key"',
    '"aws_secret_access_key": "sensitive-cloud-secret"',
    "Authorization: Basic sensitive-basic", "Cookie: session=sensitive-cookie",
    '"Authorization":"Basic sensitive-json-header"', '"Set-Cookie":"session=sensitive-cookie"',
    "Bearer sensitive-bearer", "https://user:sensitive-uri@example.com",
    "AKIA1234567890123456", "xoxb-sensitive-slack-token", "ghp_sensitivetoken",
    "github_pat_sensitive_token", "sk-sensitiveopenaitoken123",
    "-----BEGIN PRIVATE KEY-----\nsensitive-pem\n-----END PRIVATE KEY-----",
])
def test_evidence_redacts_common_credentials(secret):
    result = safe_evidence(secret)
    assert "sensitive" not in result and "AKIA" not in result
    assert "REDACTED" in result


def test_evidence_neutralizes_slack_mentions_and_code_fences():
    result = safe_evidence("<!channel> <@U123> ```oops``` & here")
    assert "<!" not in result and "<@" not in result and "```" not in result
    assert "&lt;!channel&gt;" in result and "&amp;" in result


@pytest.mark.parametrize("durable", [False, True])
def test_duplicate_signals_fetch_evidence_only_once(durable):
    collector = Mock(return_value="Recent Logs: app failed")
    instance = watcher(durable=durable, evidence=collector)
    instance.handle_pod(pod(main=[container(reason="Error")]))
    instance.handle_event(event())
    instance.handle_pod(pod(main=[container(waiting="CrashLoopBackOff")]))
    collector.assert_called_once()
    if durable:
        assert instance.db.enqueue_crash_alert.call_count == 3
        assert len({call.args[1]["message"] for call in instance.db.enqueue_crash_alert.call_args_list}) == 1
    else:
        instance.notifier.send_alert.assert_called_once()


def test_collector_failure_still_delivers_alert():
    instance = watcher(evidence=Mock(side_effect=RuntimeError("sensitive exception")))
    instance.handle_event(event())
    instance.notifier.send_alert.assert_called_once()
    assert "sensitive" not in instance.notifier.send_alert.call_args.args[2]


def test_failed_durable_enqueue_does_not_cache_or_drop_alert():
    collector = Mock(return_value="failure context")
    instance = watcher(durable=True, evidence=collector)
    instance.db.enqueue_crash_alert.side_effect = [RuntimeError("offline"), True]
    with pytest.raises(RuntimeError):
        instance.handle_event(event())
    assert instance._evidence_payloads == {}
    instance.handle_event(event())
    assert collector.call_count == 2
    instance.delivery.wake.assert_called_once()



def test_evidence_budget_skips_remaining_reads(monkeypatch):
    ticks = iter([0, 0, 5, 5])
    monkeypatch.setattr("crash_watcher.time.monotonic", lambda: next(ticks))
    client = evidence_client()
    result = collect(client)
    client.read_namespaced_pod.assert_called_once()
    client.read_namespaced_pod_log.assert_not_called()
    client.list_namespaced_event.assert_not_called()
    assert "Recent Logs: unavailable" in result
    assert "Recent Warning Events: unavailable" in result



def test_multiline_private_key_is_redacted_before_log_line_trimming():
    client = evidence_client(logs="before\n-----BEGIN RSA PRIVATE KEY-----\n" +
                             "sensitive-material\n" * 10 + "-----END RSA PRIVATE KEY-----\nafter")
    result = collect(client)
    assert "sensitive" not in result and "REDACTED PRIVATE KEY" in result
    assert "after" in result


@pytest.mark.parametrize("error", [
    ApiException(status=400, reason="private body"),
    ApiException(status=404, reason="private body"),
    ApiException(status=429, reason="private body"),
    ApiException(status=503, reason="private body"),
    ApiException(status=0, reason="private body"),
    TimeoutError("private body"),
])
def test_transient_log_failure_recovers_without_leaking_errors(monkeypatch, error):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client()
    client.read_namespaced_pod_log.side_effect = [error, "recovered traceback"]
    result = collect(client)
    assert "recovered traceback" in result and "unavailable" not in result
    assert "private body" not in result
    assert client.read_namespaced_pod.call_count == 2
    assert client.read_namespaced_pod_log.call_count == 2
    client.api_client.close.assert_called_once()


@pytest.mark.parametrize("init", [False, True])
def test_retry_refreshes_container_state_after_restart(monkeypatch, init):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    current = container(reason="Error")
    restarted = container(reason="Error", last=True, waiting="CrashLoopBackOff")
    snapshots = [pod(init=[cs]) if init else pod(main=[cs]) for cs in (current, restarted)]
    client = evidence_client()
    client.read_namespaced_pod.side_effect = snapshots
    client.read_namespaced_pod_log.side_effect = [ApiException(status=400), "crash traceback"]
    result = collect(client, observed_pod=snapshots[0])
    assert [call.kwargs["previous"] for call in client.read_namespaced_pod_log.call_args_list] == [False, True]
    assert "Recent Logs (Previous Container)" in result


def test_retry_never_reads_logs_from_replacement_pod(monkeypatch):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client()
    client.read_namespaced_pod.side_effect = [pod(main=[container(reason="Error")]), pod(uid="replacement")]
    client.read_namespaced_pod_log.side_effect = ApiException(status=400)
    result = collect(client)
    client.read_namespaced_pod_log.assert_called_once()
    assert "pod was replaced" in result


@pytest.mark.parametrize("status,reason", [(401, "authentication failed"), (403, "access denied")])
def test_log_permission_errors_are_reported_without_retry(status, reason, caplog):
    client = evidence_client()
    client.read_namespaced_pod_log.side_effect = ApiException(status=status, reason="private body")
    result = collect(client)
    client.read_namespaced_pod_log.assert_called_once()
    assert f"log read: {reason} (HTTP {status}) (1 attempt)" in result
    assert reason in caplog.text and "private body" not in caplog.text
    assert "private body" not in result


@pytest.mark.parametrize("logs", ["", " \n\t"])
def test_empty_logs_retry_then_report_no_output(monkeypatch, logs):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client(logs=logs)
    result = collect(client)
    assert client.read_namespaced_pod_log.call_count == 3
    assert "log read returned no output (3 attempts)" in result
    client.list_namespaced_event.assert_called_once()


def test_empty_logs_can_become_available_on_retry(monkeypatch):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client()
    client.read_namespaced_pod_log.side_effect = ["", "late traceback"]
    assert "late traceback" in collect(client)
    assert client.read_namespaced_pod_log.call_count == 2


def test_transient_pod_lookup_failure_retries_before_reading_logs(monkeypatch):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client()
    snapshot = client.read_namespaced_pod.return_value
    client.read_namespaced_pod.side_effect = [TimeoutError("private body"), snapshot]
    assert "failure line" in collect(client)
    assert client.read_namespaced_pod.call_count == 2
    client.read_namespaced_pod_log.assert_called_once()


def test_retry_exhaustion_reports_safe_final_reason(monkeypatch, caplog):
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: None)
    client = evidence_client()
    client.read_namespaced_pod_log.side_effect = ApiException(status=503, reason="private body")
    result = collect(client)
    assert client.read_namespaced_pod_log.call_count == 3
    assert "log read: API request failed (HTTP 503) (3 attempts)" in result
    assert "private body" not in result + caplog.text


def test_retry_stops_when_shared_evidence_budget_expires(monkeypatch):
    tick = [0.0]
    monkeypatch.setattr("crash_watcher.time.monotonic", lambda: tick[0])
    monkeypatch.setattr("crash_watcher.time.sleep", lambda _: pytest.fail("must not sleep past budget"))
    client = evidence_client()

    def failed_log_read(*args, **kwargs):
        tick[0] = 4.0
        raise TimeoutError("private body")

    client.read_namespaced_pod_log.side_effect = failed_log_read
    result = collect(client)
    client.read_namespaced_pod.assert_called_once()
    client.read_namespaced_pod_log.assert_called_once()
    client.list_namespaced_event.assert_not_called()
    assert "evidence time budget exhausted" in result
