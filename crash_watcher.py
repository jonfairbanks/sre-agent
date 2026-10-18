"""Watch pod failures continuously, independently of scheduled health checks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import re
import threading
import time

from kubernetes import watch
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError
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


_SECRET_ASSIGNMENT = re.compile(
    r'(?i)(["\']?(?:[\w-]*(?:token|password|passwd|secret|api[_-]?key|access[_-]?key)[\w-]*)["\']?\s*[:=]\s*)("[^"\n]*"|\'[^\'\n]*\'|[^\s,;]+)')
_TOKEN = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]+|gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}")


def safe_evidence(value, limit=1400):
    """Redact common secrets and prevent log text from issuing Slack mentions."""
    text = str(value or "")
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)",
                  "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$",
                  r"\1[REDACTED]", text)
    text = re.sub(r'(?i)(["\'](?:authorization|proxy-authorization|cookie|set-cookie)["\']\s*:\s*)("[^"\n]*"|\'[^\'\n]*\')',
                  r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    text = re.sub(r"(\w+://)[^\s/:@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = _TOKEN.sub("[REDACTED]", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace("`", "'")
    return text if limit is None else text[:limit]


def _termination(pod, container):
    if pod is None:
        return None, True
    for status in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
        if status.name == container:
            current = status.state.terminated if status.state else None
            previous = status.last_state.terminated if status.last_state else None
            return (current, False) if current else (previous, True)
    return None, True


def _event_stamp(event):
    stamps = [event.last_timestamp, event.event_time, event.first_timestamp,
              event.series.last_observed_time if event.series else None]
    return max((stamp for stamp in stamps if stamp is not None), default=None)


class _EvidenceBudgetExpired(TimeoutError):
    pass


def _log_read_failure(error, stage):
    """Return a safe reason and retry policy without exposing API bodies."""
    if isinstance(error, _EvidenceBudgetExpired):
        return "evidence time budget exhausted", False
    if isinstance(error, ApiException):
        status = error.status
        reasons = {401: "authentication failed", 403: "access denied",
                   404: "not found", 400: "container logs not ready",
                   429: "rate limited"}
        detail = reasons.get(status, "API request failed")
        if status == 400 and stage != "log read":
            detail = "invalid request"
        reason = f"{stage}: {detail} (HTTP {status})" if status else f"{stage}: connection failed"
        retry = (status in (0, 429) or (isinstance(status, int) and 500 <= status <= 599)
                 or (stage == "log read" and status in (400, 404)))
        return reason, retry
    return f"{stage}: {safe_error_type(error)}", isinstance(error, (OSError, HTTPError))


def collect_crash_evidence(client_factory, *, uid, namespace, pod_name, container,
                           now, observed_pod=None, observed_event=None):
    """Read a bounded snapshot. Missing evidence never blocks the crash alert."""
    deadline = time.monotonic() + 4

    def request_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            raise _EvidenceBudgetExpired("Evidence budget exhausted")
        bound = min(1, remaining / 2)
        return (bound, bound)

    client = None
    pod = None
    logs = None
    failure = "collection failed"
    attempts = 0
    events_available = False
    events = [observed_event] if observed_event is not None else []
    try:
        client = client_factory()
        for attempt in range(3):
            stage = "pod lookup"
            try:
                # Revalidate identity and container state after every transient failure.
                timeout = request_timeout()
                attempts += 1
                candidate = client.read_namespaced_pod(pod_name, namespace, _request_timeout=timeout)
                if candidate.metadata.uid != uid:
                    failure = "pod was replaced; original container logs were not requested"
                    break
                pod = candidate
                _, previous = _termination(pod, container)
                stage = "log read"
                logs = client.read_namespaced_pod_log(
                    pod_name, namespace, container=container, previous=previous,
                    tail_lines=6, limit_bytes=1400, _request_timeout=request_timeout())
                if isinstance(logs, str) and logs.strip():
                    break
                failure, retry = "log read returned no output", True
            except Exception as error:
                failure, retry = _log_read_failure(error, stage)
            if not retry or attempt == 2:
                break
            # Leave the existing shared deadline in charge of all API reads.
            remaining = deadline - time.monotonic()
            if remaining <= 0.2:
                failure += "; evidence time budget exhausted"
                break
            time.sleep(min(0.1 * (attempt + 1), remaining - 0.1))
        try:
            page = client.list_namespaced_event(
                namespace, field_selector=f"involvedObject.uid={uid},type=Warning",
                limit=10, _request_timeout=request_timeout())
            events.extend(page.items)
            events_available = True
        except Exception:
            pass
    except Exception as error:
        failure, _ = _log_read_failure(error, "client setup")
    finally:
        if client is not None:
            try:
                client.api_client.close()
            except Exception:
                pass
    term, _ = _termination(observed_pod or pod, container)
    parts = []
    if term:
        parts.append(f"Termination: {safe_evidence(term.reason or 'Error', 80)} (exit {term.exit_code}).")
    if isinstance(logs, str) and logs.strip():
        # Redact before trimming lines so a long PEM block keeps its header
        # while the redactor removes the entire secret.
        excerpt = "\n".join(safe_evidence(logs, limit=None).splitlines()[-6:])[:1400]
        source = "Previous Container" if previous else "Current Container"
        parts.append(f"Recent Logs ({source}):\n```\n{excerpt}\n```")
    else:
        count = "attempt" if attempts == 1 else "attempts"
        parts.append(f"Recent Logs: unavailable. {failure} ({attempts} {count}).")
        log.warning("Crash log collection failed (namespace=%s, pod=%s, container=%s, attempts=%s): %s",
                    namespace, pod_name, container, attempts, failure)
    recent = [e for e in events if e.involved_object.uid == uid and e.type == "Warning"
              and _recent(_event_stamp(e), now)]
    recent.sort(key=_event_stamp, reverse=True)
    unique = []
    for e in recent:
        line = safe_evidence(f"{e.reason}: {e.message}", 200)
        if line not in unique:
            unique.append(line)
        if len(unique) == 3:
            break
    if unique:
        parts.append("Recent Warning Events:\n" + "\n".join(unique))
    elif not events_available:
        parts.append("Recent Warning Events: unavailable.")
    return "\n".join(parts)[:2200]


class CrashWatcher:
    def __init__(self, db, notifier, delivery=None, *, client_factory=core_v1,
                 watch_factory=watch.Watch, clock=None, evidence_collector=None):
        self.db, self.notifier, self.delivery = db, notifier, delivery
        self.client_factory, self.watch_factory = client_factory, watch_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = []
        self._seen = {}
        self._pending = {}
        self._evidence_payloads = {}
        self.evidence_collector = evidence_collector or (
            lambda **kwargs: collect_crash_evidence(self.client_factory, **kwargs))
        self._connected = {"pods": False, "events": False}

    def status(self):
        return {"running": any(t.is_alive() for t in self._threads) and not self._stop.is_set(),
                "pods_connected": self._connected["pods"],
                "events_connected": self._connected["events"],
                "durable": self.db.available}

    def _alert(self, uid, namespace, pod_name, container, reason, restarts=None,
               *, observed_pod=None, observed_event=None):
        if not uid or not container:
            return
        key = f"{uid}/{container}"
        with self._lock:
            cutoff = self.clock() - timedelta(days=30)
            self._evidence_payloads = {k: v for k, v in self._evidence_payloads.items() if v[0] >= cutoff}
            cached = self._evidence_payloads.get(key)
            if cached:
                payload = cached[1]
            else:
                message = (f"Pod `{safe_evidence(pod_name, 150)}`, container `{safe_evidence(container, 150)}`: "
                           f"`{safe_evidence(reason, 80)}`."
                           + (f" Restarts: {restarts}." if restarts is not None else ""))
                try:
                    evidence = self.evidence_collector(
                        uid=uid, namespace=namespace, pod_name=pod_name, container=container,
                        now=self.clock(), observed_pod=observed_pod, observed_event=observed_event)
                except Exception:
                    evidence = "Recent Logs: unavailable. Evidence collection failed."
                if evidence:
                    message += "\n" + str(evidence)
                payload = {"type": "crash_alert", "severity": "critical",
                           "title": "Pod Crash Detected", "namespace": namespace,
                           "message": message[:2499]}
                # Cache only after durable enqueue succeeds below, so a failed
                # enqueue remains retryable without marking the alert delivered.
            if self.db.available:
                queued = self.db.enqueue_crash_alert(key, payload)
                self._evidence_payloads[key] = (self.clock(), payload)
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
                    self._evidence_payloads[key] = (self.clock(), payload)
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
                        container, reason, restarts, observed_pod=pod)

    def handle_event(self, event):
        ref = event.involved_object
        stamps = [event.last_timestamp, event.event_time, event.first_timestamp,
                  event.series.last_observed_time if event.series else None]
        stamp = max((stamp for stamp in stamps if stamp is not None), default=None)
        if ref.kind != "Pod" or event.type != "Warning" or not _recent(stamp, self.clock()):
            return
        # Generic BackOff also covers image pulls. Only failed-container restart
        # events trigger alerts; context is bounded and redacted by the collector.
        match = _CONTAINER.search(ref.field_path or "")
        failed = _BACKOFF_CONTAINER.search(event.message or "")
        if event.reason == "BackOff" and failed:
            container = match.group(1) if match else failed.group(1)
            self._alert(ref.uid, event.metadata.namespace, ref.name, container, "CrashLoopBackOff",
                        observed_event=event)

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
