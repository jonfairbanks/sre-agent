"""Immediate crash evidence, deduplication, and watch recovery contracts."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from kubernetes import client as k
from kubernetes.client.exceptions import ApiException

from crash_watcher import CrashWatcher, pod_crashes

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


def watcher(*, durable=False, client=None, factory=None):
    db = NS(available=durable, enqueue_crash_alert=Mock(return_value=True))
    notifier = NS(enabled=True, send_alert=Mock(return_value="slack-ts"))
    delivery = NS(wake=Mock())
    instance = CrashWatcher(db, notifier, delivery, clock=lambda: NOW,
                            client_factory=lambda: client, watch_factory=factory)
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


def test_deleted_pod_backoff_event_alerts_without_pod_lookup():
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
