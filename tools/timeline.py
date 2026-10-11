"""Bounded incident chronology from stored checks and read-only cluster evidence."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from langchain.tools import tool
from config import TOOL_OUTPUT_MAX_CHARS

from .k8s_client import core_v1, custom_objects

PAGE_LIMIT = 200
REQUEST_TIMEOUT = 15


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _text(value, length=200):
    """Redact credential patterns; source bodies and condition messages are omitted."""
    value = str(value or "")[:2000]
    value = re.sub(r"(?i)\b(bearer\s+)[\w./+\-=]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)\b(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|AKIA[A-Z0-9]{16})\b", "[redacted]", value)
    value = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "https://[redacted]@", value)
    return value[:length]


def _ref(kind, name, namespace="", uid=""):
    return {"kind": _text(kind), "namespace": _text(namespace), "name": _text(name), "uid": _text(uid)}


def _resource(obj, fallback=""):
    metadata = _get(obj, "metadata", {})
    return _ref(_get(obj, "kind", fallback), _get(metadata, "name", ""),
                _get(metadata, "namespace", ""), _get(metadata, "uid", ""))


def _items(response):
    return _get(response, "items", []) or []


def _continued(response):
    metadata = _get(response, "metadata", {})
    return bool(_get(metadata, "continue", _get(metadata, "_continue", "")))


def _failure(error):
    """Never expose API response bodies, URLs, or exception text."""
    status = getattr(error, "status", None)
    return f"Read failed ({type(error).__name__}" + (f", HTTP {status}" if status else "") + ")."


def _link(url):
    try:
        parts = urlsplit(url or "")
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            return []
        return [{"label": "Source Repository", "url": url}]
    except ValueError:
        return []


def build_incident_timeline(db, namespace="", hours=6, limit=100, fingerprint="",
                            maintenance_context="", *, now=None, core_api=None, custom_api=None):
    """Collect evidence without treating correlation or missing sources as recovery."""
    hours = max(1, min(int(hours), 168))
    limit = max(1, min(int(limit), 200))
    namespace = "" if namespace == "--all-namespaces" else namespace
    now = _time(now) or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    observations, coverage = [], []

    def add(source, observed_at, resource, reason, *, state="historical", details=None, links=None):
        observed_at = _time(observed_at)
        if observed_at is None or not since <= observed_at <= now:
            return
        observations.append({"source": source, "observed_at": observed_at.isoformat(),
                             "resource": resource, "classification": "evidence", "state": state,
                             "reason": _text(reason), "details": details or {}, "links": links or []})

    def read(source, fn, consume):
        try:
            result = fn()
            consume(result)
            coverage.append({"source": source, "status": "partial" if _continued(result) or isinstance(result, list) and len(result) >= 50 else "complete",
                             "detail": "Result cap reached; additional results may exist." if _continued(result) or isinstance(result, list) and len(result) >= 50 else ""})
        except Exception as error:
            coverage.append({"source": source, "status": "unavailable", "detail": _failure(error)})

    def consume_checks(rows):
        for row in rows:
            ts = _get(row, "observed_at", _get(row, "created_at", _get(row, "checked_at", _get(row, "ts"))))
            report = _get(row, "report", {}) or {}
            if isinstance(report, str):
                report = json.loads(report)
            add("monitor_checks", ts, _ref("Cluster", "monitor", namespace), "Monitor Check",
                state="recorded", details={"severity": _text(_get(report, "overall_severity", "unknown")),
                                           "coverage": [{"area": _text(_get(c, "area", "")), "status": _text(_get(c, "status", "unknown"))}
                                                        for c in (_get(row, "coverage", []) or [])],
                                           "analysis_valid": bool(_get(row, "analysis_valid", True)),
                                           "session_id": _text(_get(row, "session_id", "")),
                                           "check_no": _get(row, "check_no")})
            for finding in (_get(report, "findings", []) or []) if _get(row, "analysis_valid", True) else []:
                if namespace and _get(finding, "namespace", "") != namespace:
                    continue
                add("monitor_checks", ts, _ref(_get(finding, "kind", ""), _get(finding, "resource_name", ""),
                                               _get(finding, "namespace", "")), _get(finding, "reason", "Finding"),
                    state="recorded", details={"severity": _text(_get(finding, "severity", "unknown"))})
            diff = _get(row, "diff", {}) or {}
            if isinstance(diff, str):
                diff = json.loads(diff)
            for finding in (_get(diff, "resolved", []) or []) if _get(row, "analysis_valid", True) else []:
                if namespace and _get(finding, "namespace", "") != namespace:
                    continue
                add("monitor_checks", ts, _ref(_get(finding, "kind", ""), _get(finding, "resource_name", ""),
                                               _get(finding, "namespace", "")), _get(finding, "reason", "Finding Resolved"),
                    state="resolved", details={"resolution_source": "persisted monitor diff"})

    if db is None or not getattr(db, "available", True):
        coverage.append({"source": "monitor_checks", "status": "unavailable", "detail": "Persistence is unavailable."})
        if fingerprint:
            coverage.append({"source": "finding_history", "status": "unavailable", "detail": "Persistence is unavailable."})
    else:
        read("monitor_checks", lambda: db.recent_monitor_checks(since=since, namespace=namespace, limit=50), consume_checks)
        if fingerprint:
            def consume_history(rows):
                for row in rows:
                    finding = _get(row, "finding", row)
                    if namespace and _get(finding, "namespace", "") != namespace:
                        continue
                    add("finding_history", _get(row, "observed_at", _get(row, "created_at", _get(row, "ts"))),
                        _ref(_get(finding, "kind", ""), _get(finding, "resource_name", ""), _get(finding, "namespace", "")),
                        _get(row, "reason", "Finding Observation"), state=_get(row, "status", "recorded"),
                        details={"fingerprint": _text(fingerprint), "severity": _text(_get(finding, "severity", ""))})
            read("finding_history", lambda: db.finding_history(fingerprint, limit=50), consume_history)

    try:
        core_api = core_api or core_v1()
    except Exception as error:
        coverage.append({"source": "kubernetes", "status": "unavailable", "detail": _failure(error)})
    else:
        pods, nodes = [], []
        read("pods", lambda: (core_api.list_namespaced_pod(namespace, limit=PAGE_LIMIT, _request_timeout=REQUEST_TIMEOUT)
                              if namespace else core_api.list_pod_for_all_namespaces(limit=PAGE_LIMIT, _request_timeout=REQUEST_TIMEOUT)),
             lambda result: pods.extend(_items(result)))
        read("nodes", lambda: core_api.list_node(limit=PAGE_LIMIT, _request_timeout=REQUEST_TIMEOUT),
             lambda result: nodes.extend(_items(result)))
        current = {}
        for pod in pods:
            ref = _resource(pod, "Pod")
            status = _get(pod, "status", {})
            phase = _get(status, "phase", "Unknown")
            ready = any(_get(c, "type") == "Ready" and _get(c, "status") == "True"
                        for c in _get(status, "conditions", []) or [])
            current[("Pod", ref["namespace"], ref["name"])] = {"phase": phase, "ready": ready,
                                                                       "node": _get(_get(pod, "spec", {}), "node_name", ""), "uid": ref["uid"]}
            owner = next(iter(_get(_get(pod, "metadata", {}), "owner_references", []) or []), None)
            for container in (_get(status, "container_statuses", []) or []) + (_get(status, "init_container_statuses", []) or []):
                terminated = _get(_get(container, "last_state", {}), "terminated")
                if terminated:
                    add("pod_restarts", _get(terminated, "finished_at"), ref, "Container Restart",
                        details={"container": _text(_get(container, "name")), "restart_count": _get(container, "restart_count", 0),
                                 "termination_reason": _text(_get(terminated, "reason")), "exit_code": _get(terminated, "exit_code"),
                                 "current_ready": _get(container, "ready", False),
                                 "owner": _ref(_get(owner, "kind", ""), _get(owner, "name", ""), ref["namespace"])})
        for node in nodes:
            ref = _resource(node, "Node")
            conditions = _get(_get(node, "status", {}), "conditions", []) or []
            for condition in conditions:
                if _get(condition, "type") not in ("Ready", "DiskPressure", "MemoryPressure", "PIDPressure", "NetworkUnavailable"):
                    continue
                ready = _get(condition, "status")
                impacts = [_resource(p, "Pod") for p in pods if _get(_get(p, "spec", {}), "node_name") == ref["name"]]
                add("node_conditions", now, ref, _get(condition, "type"),
                    state="current", details={"status": ready, "reason": _text(_get(condition, "reason")),
                                              "last_transition_at": (_time(_get(condition, "last_transition_time")).isoformat()
                                                                     if _time(_get(condition, "last_transition_time")) else None),
                                              "resident_pods": impacts[:20], "resident_pods_truncated": len(impacts) > 20,
                                              "resident_pods_scope": namespace or "first cluster pod page",
                                              "impact_note": "These pods are currently assigned to the node. Assignment alone does not establish workload impact."})
            current[("Node", "", ref["name"])] = {"ready": any(_get(c, "type") == "Ready" and _get(c, "status") == "True" for c in conditions), "uid": ref["uid"]}

        def consume_events(result):
            for event in _items(result):
                obj = _get(event, "involved_object", {})
                ref = _ref(_get(obj, "kind", ""), _get(obj, "name", ""), _get(obj, "namespace", ""), _get(obj, "uid", ""))
                series = _get(event, "series", {})
                ts = (_get(series, "last_observed_time") or _get(event, "last_timestamp")
                      or _get(event, "event_time") or _get(event, "first_timestamp")
                      or _get(_get(event, "metadata", {}), "creation_timestamp"))
                snapshot = current.get((ref["kind"], ref["namespace"], ref["name"]))
                if snapshot and ref["uid"] and snapshot.get("uid") and snapshot["uid"] != ref["uid"]:
                    snapshot = None
                add("kubernetes_events", ts, ref, _get(event, "reason", "Event"),
                    details={"type": _text(_get(event, "type", "")), "count": _get(series, "count", _get(event, "count", 1)),
                             "current_resource_state": snapshot,
                             "state_note": "Historical event; current resource state does not prove that this event is active or resolved."})
        read("kubernetes_events", lambda: (core_api.list_namespaced_event(namespace, limit=PAGE_LIMIT, _request_timeout=REQUEST_TIMEOUT)
                                          if namespace else core_api.list_event_for_all_namespaces(limit=PAGE_LIMIT, _request_timeout=REQUEST_TIMEOUT)), consume_events)

    try:
        custom_api = custom_api or custom_objects()
    except Exception as error:
        coverage.append({"source": "custom_resources", "status": "unavailable", "detail": _failure(error)})
    else:
        def list_custom(group, version, plural, scoped=True):
            kwargs = {"limit": PAGE_LIMIT, "_request_timeout": REQUEST_TIMEOUT}
            if namespace and scoped:
                return custom_api.list_namespaced_custom_object(group, version, namespace, plural, **kwargs)
            return custom_api.list_cluster_custom_object(group, version, plural, **kwargs)

        def consume_argo(result):
            for app in _items(result):
                status, spec = app.get("status", {}), app.get("spec", {})
                destination = spec.get("destination", {})
                if namespace and destination.get("namespace") != namespace:
                    continue
                ref = _resource(app, "Application")
                sources = spec.get("sources") or [spec.get("source", {})]
                links = [link for source in sources for link in _link(source.get("repoURL"))]
                add("argo_rollouts", now, ref, "Argo Application Snapshot", state="current",
                    details={"sync_status": _text(status.get("sync", {}).get("status")),
                             "health": _text(status.get("health", {}).get("status")),
                             "revision": _text(status.get("sync", {}).get("revision")),
                             "destination_namespace": _text(destination.get("namespace"))}, links=links)
                for deployment in status.get("history", [])[-20:]:
                    add("argo_rollouts", deployment.get("deployedAt"), ref, "Argo Deployment Recorded",
                        details={"revision": _text(deployment.get("revision")),
                                 "revisions": [_text(r) for r in deployment.get("revisions", [])[:10]],
                                 "destination_namespace": _text(destination.get("namespace"))}, links=links)
                operation = status.get("operationState", {})
                if operation:
                    add("argo_rollouts", operation.get("finishedAt") or operation.get("startedAt"), ref, "Argo Operation",
                        details={"phase": _text(operation.get("phase")), "sync_status": _text(status.get("sync", {}).get("status")),
                                 "health": _text(status.get("health", {}).get("status")),
                                 "revision": _text(operation.get("syncResult", {}).get("revision"))}, links=links)
        read("argo_rollouts", lambda: list_custom("argoproj.io", "v1alpha1", "applications", scoped=False), consume_argo)

        def consume_vault(result):
            for obj in _items(result):
                ref = _resource(obj, "VaultResource")
                generation = obj.get("metadata", {}).get("generation")
                for condition in obj.get("status", {}).get("conditions", []):
                    observed = condition.get("observedGeneration", obj.get("status", {}).get("observedGeneration"))
                    freshness = "unknown" if observed is None or generation is None else "current" if observed >= generation else "stale"
                    transition = condition.get("lastTransitionTime")
                    add("vault_conditions", now, ref, "Vault Condition Snapshot", state="current",
                        details={"type": _text(condition.get("type")), "status": _text(condition.get("status")),
                                 "reason": _text(condition.get("reason")), "generation": generation,
                                 "observed_generation": observed, "freshness": freshness,
                                 "last_transition_at": _time(transition).isoformat() if _time(transition) else None})
        for plural in ("vaultstaticsecrets", "vaultdynamicsecrets", "vaultauths", "vaultconnections"):
            read(f"vault_conditions/{plural}", lambda plural=plural: list_custom("secrets.hashicorp.com", "v1beta1", plural), consume_vault)

    if maintenance_context:
        add("operator_context", now, _ref("Cluster", "operator", namespace), "Supplied Maintenance Context", state="recorded",
            details={"context": _text(maintenance_context, 500), "verified": False})
    unique = {}
    for observation in observations:
        key = json.dumps(observation, sort_keys=True, default=str)
        unique[key] = observation
    ordered = sorted(unique.values(), key=lambda row: (row["observed_at"], row["source"], row["resource"]["name"]))
    return {"window": {"since": since.isoformat(), "until": now.isoformat(), "hours": hours},
            "namespace": namespace, "observations": ordered[-limit:], "coverage": coverage,
            "truncated": len(ordered) > limit, "omitted_observations": max(0, len(ordered) - limit),
            "interpretation": "Chronology records observations. Correlated changes do not establish causes or suppress independent findings. Events may expire before this window; current snapshots are not historical availability proof."}


def _bounded_result(payload):
    """Keep complete JSON and newest evidence within the agent's character budget."""
    result = dict(payload)
    result["observations"] = list(payload["observations"])
    result["coverage"] = [dict(row) for row in payload["coverage"]]
    result["coverage_truncated"] = False
    result["omitted_coverage"] = 0
    budget = max(1, TOOL_OUTPUT_MAX_CHARS - min(128, TOOL_OUTPUT_MAX_CHARS // 10))

    def encode():
        return json.dumps(result, default=str, separators=(",", ":"))

    encoded = encode()
    if len(encoded) <= budget:
        return encoded
    result["truncated"] = True
    result["coverage"].append({"source": "timeline_output", "status": "partial",
                               "detail": "Output capped; oldest observations or source coverage omitted."})
    while result["observations"] and len(encode()) > budget:
        result["observations"].pop(0)
        result["omitted_observations"] += 1
    # Keep unavailable/partial source coverage before successful collection rows.
    result["coverage"].sort(key=lambda row: {"unavailable": 0, "partial": 1, "complete": 2}.get(row["status"], 2))
    while result["coverage"] and len(encode()) > budget:
        result["coverage"].pop()
        result["coverage_truncated"] = True
        result["omitted_coverage"] += 1
    encoded = encode()
    if len(encoded) <= budget:
        return encoded
    # A very small configured budget can exclude even the window/source metadata.
    compact = {"observations": [], "truncated": True,
               "omitted_observations": result["omitted_observations"],
               "coverage_truncated": True, "omitted_coverage": len(payload["coverage"])}
    encoded = json.dumps(compact, separators=(",", ":"))
    if len(encoded) <= budget:
        return encoded
    compact = {"omitted_observations": result["omitted_observations"], "coverage_truncated": True}
    encoded = json.dumps(compact, separators=(",", ":"))
    if len(encoded) <= budget:
        return encoded
    # Remain valid JSON even when the configured limit cannot hold count metadata.
    return "null"


def make_incident_timeline_tools(db):
    @tool
    def incident_timeline(namespace: str = "", hours: int = 6, limit: int = 100,
                          fingerprint: str = "", maintenance_context: str = "") -> str:
        """Read a bounded incident/change timeline from monitor history, Kubernetes and Argo/VSO evidence.

        hours is clamped to 1-168 and limit to 1-200. Missing sources are reported
        separately. Maintenance context is unverified operator input, never cause proof.
        Historical events do not establish current failure. No resource or secret bodies
        are returned. Optional fingerprint adds that finding's persisted transition history.
        """
        return _bounded_result(build_incident_timeline(db, namespace, hours, limit, fingerprint, maintenance_context))
    return [incident_timeline]
