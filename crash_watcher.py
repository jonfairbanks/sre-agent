"""Watch pod failures continuously, independently of scheduled health checks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
import threading

from kubernetes import watch
from kubernetes.client.exceptions import ApiException
from slack_health import safe_error_type
from tools.k8s_client import core_v1

log = logging.getLogger("sre-agent.crash-watcher")
_CRASH_REASONS = {"Error", "OOMKilled", "ContainerCannotRun"}
_CONTAINER = re.compile(r"(?:initContainers|containers)\{([^}]+)\}")
_BACKOFF_CONTAINER = re.compile(r"restarting failed container ([a-zA-Z0-9_.-]+)")


def _recent(stamp, now):
    return stamp is not None and timedelta(0) <= now - stamp <= timedelta(minutes=5)


def pod_crashes(pod, now):
    """Current failures include init containers, with no startup grace period."""
    if pod.metadata.deletion_timestamp or pod.status.phase == "Succeeded":
        return []
    crashes = []
    for cs in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
        state = cs.state
        waiting = state.waiting if state else None
        term = state.terminated if state else None
        last = cs.last_state.terminated if cs.last_state else None
        reason = None
        if waiting and waiting.reason == "CrashLoopBackOff":
            reason = "CrashLoopBackOff"
        elif term and (term.reason in _CRASH_REASONS or (term.exit_code or 0) != 0):
            if _recent(term.finished_at, now):
                reason = term.reason or "Error"
        elif not cs.ready and last and _recent(last.finished_at, now):
            if last.reason in _CRASH_REASONS or (last.exit_code or 0) != 0:
                reason = last.reason or "Error"
        if reason:
            crashes.append((cs.name, reason, cs.restart_count or 0))
    return crashes


class CrashWatcher:
    def __init__(self, db, notifier, delivery=None, *, client_factory=core_v1,
                 watch_factory=watch.Watch, clock=None):
        self.db, self.notifier, self.delivery = db, notifier, delivery
        self.client_factory, self.watch_factory = client_factory, watch_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = []
        self._seen = {}
        self._pending = {}
        self._connected = {"pods": False, "events": False}

    def status(self):
        return {"running": any(t.is_alive() for t in self._threads) and not self._stop.is_set(),
                "pods_connected": self._connected["pods"],
                "events_connected": self._connected["events"],
                "durable": self.db.available}

    def _alert(self, uid, namespace, pod_name, container, reason, restarts=None):
        if not uid or not container:
            return
        key = f"{uid}/{container}"
        payload = {"type": "crash_alert", "severity": "critical",
                   "title": "Pod Crash Detected", "namespace": namespace,
                   "message": f"Pod `{pod_name}`, container `{container}`: `{reason}`."
                   + (f" Restarts: {restarts}." if restarts is not None else "")}
        with self._lock:
            if self.db.available:
                queued = self.db.enqueue_crash_alert(key, payload)
                if queued:
                    log.info("Crash alert queued (namespace=%s, pod=%s, container=%s)",
                             namespace, pod_name, container)
                # Wake on duplicates too, in case the first enqueue succeeded
                # just before a disconnect prevented the wake call.
                if self.delivery is not None:
                    self.delivery.wake()
            else:
                cutoff = self.clock() - timedelta(days=30)
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
                if key not in self._seen:
                    self._pending.setdefault(key, payload)
                    self._retry_memory()

    def _retry_memory(self):
        # Called with the lock held. Retain failed posts even if the pod disappears.
        if not self.notifier.enabled:
            return
        for key, payload in list(self._pending.items())[:10]:
            if self._stop.is_set():
                break
            try:
                ts = self.notifier.send_alert(payload["severity"], payload["title"],
                                              payload["message"], namespace=payload["namespace"])
                if ts:
                    self._seen[key] = self.clock()
                    del self._pending[key]
            except Exception as error:
                log.warning("Crash delivery deferred (%s)", safe_error_type(error))

    def handle_pod(self, pod):
        for container, reason, restarts in pod_crashes(pod, self.clock()):
            self._alert(pod.metadata.uid, pod.metadata.namespace, pod.metadata.name,
                        container, reason, restarts)

    def handle_event(self, event):
        ref = event.involved_object
        stamps = [event.last_timestamp, event.event_time, event.first_timestamp,
                  event.series.last_observed_time if event.series else None]
        stamp = max((stamp for stamp in stamps if stamp is not None), default=None)
        if ref.kind != "Pod" or event.type != "Warning" or not _recent(stamp, self.clock()):
            return
        # Generic BackOff also covers image pulls. Only failed-container restart
        # events are crash evidence. Do not copy arbitrary event messages to Slack.
        match = _CONTAINER.search(ref.field_path or "")
        failed = _BACKOFF_CONTAINER.search(event.message or "")
        if event.reason == "BackOff" and failed:
            container = match.group(1) if match else failed.group(1)
            self._alert(ref.uid, event.metadata.namespace, ref.name, container, "CrashLoopBackOff")

    def _consume(self, kind):
        client = None
        method = None
        handle = self.handle_pod if kind == "pods" else self.handle_event
        kwargs = {} if kind == "pods" else {"field_selector": "type=Warning"}
        version = None
        delay = 1
        try:
            while not self._stop.is_set():
                stream = None
                try:
                    if client is None:
                        client = self.client_factory()
                        method = (client.list_pod_for_all_namespaces if kind == "pods"
                                  else client.list_event_for_all_namespaces)
                    if version is None:
                        page = method(_request_timeout=(5, 10), **kwargs)
                        for obj in page.items:
                            if self._stop.is_set():
                                return
                            handle(obj)
                        version = page.metadata.resource_version
                    stream = self.watch_factory()
                    self._connected[kind] = True
                    for change in stream.stream(method, resource_version=version,
                                                timeout_seconds=20, _request_timeout=(5, 25), **kwargs):
                        if self._stop.is_set():
                            return
                        obj = change["object"]
                        if change["type"] == "ERROR":
                            raise ApiException(status=int(getattr(obj, "code", 0) or 0))
                        if change["type"] != "BOOKMARK" and change["type"] != "DELETED":
                            handle(obj)
                        version = obj.metadata.resource_version
                        delay = 1
                    self._connected[kind] = False
                    # Guard against servers closing watches immediately.
                    self._stop.wait(0.2)
                except Exception as error:
                    self._connected[kind] = False
                    if isinstance(error, ApiException) and error.status == 410:
                        version = None
                    log.warning("%s watch disconnected (%s); retrying", kind, safe_error_type(error))
                    self._stop.wait(delay)
                    delay = min(delay * 2, 30)
                finally:
                    if stream is not None:
                        stream.stop()
        finally:
            self._connected[kind] = False
            if client is not None:
                client.api_client.close()

    def _memory_delivery(self):
        while not self._stop.wait(15):
            with self._lock:
                self._retry_memory()

    async def start(self):
        if self._threads:
            return
        self._stop.clear()
        for kind in ("pods", "events"):
            thread = threading.Thread(target=self._consume, args=(kind,), daemon=True,
                                      name=f"crash-watch-{kind}")
            self._threads.append(thread)
            thread.start()
        if not self.db.available:
            thread = threading.Thread(target=self._memory_delivery, daemon=True,
                                      name="crash-delivery")
            self._threads.append(thread)
            thread.start()
        log.info("Continuous pod and event monitoring started")

    async def stop(self):
        self._stop.set()
        await asyncio.gather(*(asyncio.to_thread(t.join) for t in self._threads))
        self._threads.clear()
