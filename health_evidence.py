"""Snapshot facts own incident identity; model prose adds context."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from health_history import recovered_termination_details
from monitor_state import canonicalize_stored_findings, fingerprint
from schemas import CollectionCoverage, Finding, HealthReport

DISK_ENTER_PERCENT = float(os.getenv("PVC_USAGE_ALERT_PERCENT", "70"))
DISK_CLEAR_PERCENT = float(os.getenv("DISK_USAGE_CLEAR_PERCENT", "65"))
DISK_CRITICAL_CLEAR_PERCENT = float(os.getenv("DISK_USAGE_CRITICAL_CLEAR_PERCENT", "85"))
DISK_CRITICAL_PERCENT = float(os.getenv("DISK_USAGE_CRITICAL_PERCENT", "90"))
if not (0 <= DISK_CLEAR_PERCENT < DISK_ENTER_PERCENT <= DISK_CRITICAL_PERCENT <= 100
        and DISK_ENTER_PERCENT <= DISK_CRITICAL_CLEAR_PERCENT < DISK_CRITICAL_PERCENT):
    raise ValueError("Disk thresholds must satisfy 0 <= clear < enter <= critical <= 100")


def collection_coverage(data: dict) -> list[CollectionCoverage]:
    """Errors annotate areas instead of turning empty results into healthy facts."""
    areas = {
        "nodes": "nodes", "pods": "pods", "events": "events", "hpas": "hpas",
        "deployments": "deployments", "node_metrics": "node metrics",
        "pod_metrics": "pod metrics", "storage": "pvc usage",
        "volume_sources": "volume sources", "keda": "KEDA",
    }
    coverage = []
    for area, prefix in areas.items():
        errors = [error for error in data.get("errors", []) if error.startswith(prefix)]
        present = area in data or area in ("keda", "volume_sources", "storage")
        status = "partial" if errors else "complete" if present else "unavailable"
        coverage.append(CollectionCoverage(area=area, status=status,
                                           detail="; ".join(errors)))
    if any(node.get("status") == "Unknown" for node in data.get("nodes", [])):
        coverage.append(CollectionCoverage(area="node_readiness", status="partial",
                                           detail="At least one node Ready condition is unknown."))
    return coverage



def _alert_local_time(source: datetime) -> datetime:
    """Use pod configuration, then the system timezone at the event's date."""
    for setting in ("ALERT_TIMEZONE", "TZ"):
        name = os.getenv(setting, "").strip()
        if not name:
            continue
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return source.astimezone(zone)
    try:
        return source.astimezone()
    except (OSError, ValueError, OverflowError):
        return source.astimezone(timezone.utc)


def _termination_detail(record: dict) -> str:
    """Render source time separately from the Kubernetes reason token."""
    container = record.get("container") or "(name unavailable)"
    stamp = record.get("finished_at")
    finished = None
    if isinstance(stamp, str) and len(stamp) <= 80:
        try:
            finished = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if finished.utcoffset() is None:
                finished = None
        except (ValueError, OverflowError):
            pass
    if finished is None:
        text = f"Container {container}: termination time unavailable."
    else:
        exact = finished.astimezone(timezone.utc).isoformat()
        try:
            local = _alert_local_time(finished)
            clock = local.strftime("%I:%M:%S %p").lstrip("0")
            when = f"{local.strftime('%b')} {local.day}, {local.year:04d} at {clock} {local.tzname()}"
        except (ValueError, OverflowError):
            when = exact
        age = record.get("minutes_ago")
        age_text = f" ({age}m ago)" if isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= 5_256_000 else ""
        text = f"Container {container} last terminated {when}{age_text}."
    reason = record.get("reason")
    if reason == "Unknown":
        text += " Kubernetes reason: Unknown (no specific reason supplied)."
    elif reason:
        text += f" Kubernetes reason: {reason}."
    else:
        text += " Kubernetes reason unavailable."
    return text


def _pod_evidence_detail(pod: dict) -> str:
    current = pod.get("current_termination_records") or []
    current_failures = [record for record in current if record.get("reason") in
                        {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded", "Evicted"}
                        or (record.get("reason") != "Completed" and (record.get("exit_code") or 0) != 0)]
    records = current_failures or current or pod.get("last_termination_records") or []
    if not records and any(pod.get(key) is not None for key in
                           ("last_termination", "last_exit_code", "last_termination_finished_at")):
        records = [{"container": pod.get("last_termination_container"),
                    "reason": pod.get("last_termination"), "exit_code": pod.get("last_exit_code"),
                    "signal": pod.get("last_signal"), "finished_at": pod.get("last_termination_finished_at"),
                    "minutes_ago": pod.get("last_termination_min_ago")}]
    if not records:
        return f"Current pod state: {pod['status']}. No container termination evidence is available."
    details = [_termination_detail(record) for record in records[:4]]
    if len(records) > 4:
        details.append(f"{len(records) - 4} other containers share this latest termination observation.")
    return f"Current pod state: {pod['status']}. " + " ".join(details)


def reconcile_report(report: HealthReport, data: dict, stored: dict | None = None) -> HealthReport:
    """Derive factual findings and preserve independent model observations."""
    aliases = dict(data.get("filesystem_aliases", {}))
    # Reuse a confirmed identity only when today's topology lookup failed.
    # Fresh verified mount evidence owns identity after a path moves disks.
    known_aliases = data.get("known_filesystem_aliases", {})
    for key, usage in data.get("local_filesystem_usage", {}).items():
        if not usage.get("topology_verified", False):
            old = f"-/node/{usage['node']}:{usage.get('filesystem_id', key)}:localfilesystemusage".lower()
            if old in known_aliases:
                aliases[old] = known_aliases[old]
    stored = canonicalize_stored_findings(stored or {}, aliases)
    facts = []
    disk_observations = set()

    def add(kind, name, namespace, reason, severity, detail, owner_kind="", owner_name=""):
        label = {"NodeFilesystemUsage": "Node Filesystem Usage", "PVCUsage": "PVC Usage",
                 "LocalFilesystemUsage": "Local Filesystem Usage", "NotReady": "Not Ready",
                 "KEDAActivationFailure": "KEDA Activation Failure"}.get(reason, reason)
        finding = Finding(kind=kind, resource_name=name, namespace=namespace, reason=reason,
                          severity=severity, title=f"{label} on {name}", detail=detail,
                          evidence_source="snapshot", owner_kind=owner_kind, owner_name=owner_name)
        facts.append(finding)
        return finding

    for node in data.get("nodes", []):
        if node.get("status") == "NotReady":
            add("Node", node["name"], "", "NotReady", "critical", "Node Ready condition is not True.")
        for reason in ("DiskPressure", "MemoryPressure", "PIDPressure"):
            if node.get("conditions", {}).get(reason) == "True":
                add("Node", node["name"], "", reason, "critical", f"Node {reason} condition is True.")
    for pod in data.get("unhealthy_pods", []):
        reason = pod.get("status", "NotReady").removeprefix("Restarted/")
        add("Pod", pod["name"], pod["namespace"], reason, "critical",
            _pod_evidence_detail(pod),
            pod.get("owner_kind", ""), pod.get("owner_name", ""))
        if pod.get("status") == "Restarted/Unknown":
            facts[-1].title = f"Recent Container Restart on {pod['name']}"
    for hpa in data.get("hpas", []):
        keda = hpa.get("keda") or {}
        for condition in hpa.get("conditions", []):
            reason = condition.get("reason") or "ScalingFailure"
            failed = (condition.get("type") == "AbleToScale" and condition.get("status") == "False"
                      or condition.get("type") == "ScalingActive" and condition.get("status") == "False"
                      and not (keda.get("idle") and reason == "ScalingDisabled"))
            # A configured replica cap may be intentional, including during load tests.
            # ScalingLimited/TooManyReplicas alone does not establish workload harm.
            if failed:
                add("HPA", hpa["name"], hpa["namespace"], reason, "warning",
                    f"{condition['type']}={condition['status']}/{reason}.")
        faults = [c for c in keda.get("conditions", [])
                  if c.get("type") == "Ready" and c.get("status") == "False"
                  or c.get("type") == "Fallback" and c.get("status") == "True"]
        failing = any(v.get("status") == "Failing" or v.get("numberOfFailures", 0) > 0
                      for v in keda.get("health", {}).values())
        if faults or failing:
            add("HPA", hpa["name"], hpa["namespace"], "KEDAActivationFailure", "warning",
                "KEDA reports an unready scaler, active fallback, or scaler failures.")
    for deployment in data.get("deployments", []):
        if (deployment.get("settled") and deployment.get("available", 0) < deployment.get("desired", 0)):
            add("Deployment", deployment["name"], deployment["namespace"], "Unavailable", "critical",
                f"{deployment['available']}/{deployment['desired']} replicas available after startup grace.")

    disk_objects = {("persistentvolumeclaim", *key.split("/", 1))
                    for usage in data.get("node_disk_usage", {}).values()
                    for key in usage.get("local_claims", [])}
    disk_objects.update(("persistentvolumeclaim", *key.split("/", 1))
                        for key in data.get("local_filesystem_usage", {}))
    for area, kind, reason in (("node_disk_usage", "Node", "NodeFilesystemUsage"),
                               ("pvc_usage", "PersistentVolumeClaim", "PVCUsage"),
                               ("local_filesystem_usage", "Node", "LocalFilesystemUsage")):
        for key, usage in sorted(data.get(area, {}).items(), key=lambda item: item[1].get("percent") or 0, reverse=True):
            namespace, name = (key.split("/", 1) if kind == "PersistentVolumeClaim" else ("", usage.get("node", key)))
            disk_objects.add((kind.lower(), namespace, name))
            display_name = name
            if area == "local_filesystem_usage":
                name = f"{usage['node']}:{usage.get('filesystem_id', key)}"
                display_name = f"{usage['node']} ({usage.get('mountpoint') or usage.get('path') or key})"
            disk_objects.add((kind.lower(), namespace, name))
            candidate = Finding(severity="warning", title="Disk Usage", detail="", kind=kind,
                                resource_name=name, namespace=namespace, reason=reason)
            candidate_fp = fingerprint(candidate)
            if candidate_fp in aliases:
                candidate_fp = aliases[candidate_fp]
                identity, _, canonical_reason = candidate_fp.rpartition(":")
                ns, canonical_kind, name = identity.split("/", 2)
                namespace = "" if ns == "-" else ns
                kind = {"node": "Node", "persistentvolumeclaim": "PersistentVolumeClaim"}[canonical_kind]
                candidate = candidate.model_copy(update={"resource_name": name, "namespace": namespace, "kind": kind,
                    "reason": {"nodefilesystemusage": "NodeFilesystemUsage", "localfilesystemusage": "LocalFilesystemUsage", "pvcusage": "PVCUsage"}[canonical_reason]})
                display_name = name
            previous = stored.get(candidate_fp)
            open_previous = previous is not None and previous.resolved_at is None
            percent = usage.get("percent")
            if not isinstance(percent, (int, float)):
                continue
            already_observed = candidate_fp in disk_observations
            disk_observations.add(candidate_fp)
            if already_observed:
                continue
            threshold = DISK_CLEAR_PERCENT if open_previous else DISK_ENTER_PERCENT
            if percent >= threshold:
                severity = "critical" if percent >= DISK_CRITICAL_PERCENT else "warning"
                if open_previous and previous.severity == "critical" and percent >= DISK_CRITICAL_CLEAR_PERCENT:
                    severity = "critical"
                qualifier = " Shared filesystem usage is not PVC data size." if kind == "Node" else ""
                finding = add(kind, name, namespace, candidate.reason, severity, f"Filesystem usage is {percent}%.{qualifier}")
                if candidate.reason == "LocalFilesystemUsage":
                    finding.title = f"Local Filesystem Usage on {display_name}"

    fact_ids = {fingerprint(f) for f in facts}
    fact_resources = {(f.kind.lower(), f.namespace, f.resource_name) for f in facts}
    known_pods = {(p["namespace"], p["name"]) for p in data.get("pods", [])}
    recovered_resources = set()
    for pod in data.get("pods", []):
        if pod.get("status") == "Running" and pod.get("ready") is True:
            identity = Finding(severity="info", title="", detail="", kind="Pod", namespace=pod["namespace"],
                               resource_name=pod["name"], owner_kind=pod.get("owner_kind", ""),
                               owner_name=pod.get("owner_name", ""), reason="History")
            recovered_resources.add(fingerprint(identity).rsplit(":", 1)[0])
            direct = identity.model_copy(update={"owner_kind": "", "owner_name": ""})
            recovered_resources.add(fingerprint(direct).rsplit(":", 1)[0])
    known_hpas = {(h["namespace"], h["name"]): h for h in data.get("hpas", [])}
    pod_reasons = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                   "CreateContainerConfigError", "CreateContainerError", "ErrImageNeverPull",
                   "Failed", "Pending", "NotReady", "OOMKilled", "Error", "Unknown", "ContainerCannotRun",
                   "DeadlineExceeded", "Evicted"}
    model = []
    dropped_history = False
    for finding in report.findings:
        if finding.evidence_source == "snapshot":
            continue
        resource = (finding.kind.lower(), finding.namespace, finding.resource_name)
        recovered = fingerprint(finding).rsplit(":", 1)[0] in recovered_resources
        restart_claim = (finding.reason.removeprefix("Restarted/") in {"Unknown", "Error", "OOMKilled"}
                         or "restart" in finding.reason.lower() or "termination" in finding.reason.lower()
                         or "restart" in finding.title.lower())
        if recovered and restart_claim:
            dropped_history = True
            continue
        if (finding.kind.lower() == "pod" and (finding.namespace, finding.resource_name) in known_pods
                and finding.reason.removeprefix("Restarted/") in pod_reasons):
            continue
        if (finding.kind.lower() == "hpa" and (finding.namespace, finding.resource_name) in known_hpas
                and (finding.reason.startswith("Failed") or finding.reason in
                     {"ScalingDisabled", "HPAAtMaxReplicas", "TooManyReplicas", "KEDAActivationFailure"})):
            continue
        text = f"{finding.reason} {finding.title} {finding.detail}".lower()
        hpa = known_hpas.get((finding.namespace, finding.resource_name)) if finding.kind.lower() == "hpa" else None
        cap_claim = "scalinglimited" in text or ("max" in text and ("replica" in text or "scal" in text))
        if hpa and hpa.get("current") == hpa.get("max") and cap_claim:
            continue
        disk_claim = any(word in text for word in ("disk", "filesystem", "pvcusage", "volume usage", "volume utilization", "storage utilization"))
        if fingerprint(finding) in fact_ids or (disk_claim and resource in disk_objects):
            continue
        # Snapshot-backed pod/node/scaling incidents cannot be reclassified by prose.
        if resource in fact_resources and any(word in text for word in ("oom", "crash", "notready", "scaling", "activation")):
            continue
        model.append(finding)
    findings = facts + model
    coverage = collection_coverage(data)
    for fp in sorted(disk_observations):
        coverage.append(CollectionCoverage(area=f"disk:{fp}", status="complete"))
    disk_reasons = {"nodefilesystemusage", "pvcusage", "localfilesystemusage"}
    for fp, previous in stored.items():
        if previous.resolved_at is None and fp.rsplit(":", 1)[-1] in disk_reasons and fp not in disk_observations:
            coverage.append(CollectionCoverage(area=f"disk:{fp}", status="unavailable",
                                               detail="Previously observed filesystem was not sampled."))
    incomplete = any(c.status != "complete" for c in coverage) or not report.analysis_valid
    severity = max((f.severity for f in findings), key={"info": 1, "warning": 2, "critical": 3}.get, default="unknown" if incomplete else "ok")
    summary = report.summary
    actions = report.recommended_actions
    if dropped_history:
        summary = f"{len(findings)} active findings remain; highest severity is {severity}." if findings else "No active issues found."
        actions = []
    if facts:
        counts = {level: sum(f.severity == level for f in findings) for level in ("critical", "warning", "info")}
        levels = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
        summary = f"{len(findings)} active finding{'s' if len(findings) != 1 else ''}: {levels}."
        ready_nodes = sum(node.get("status") == "Ready" for node in data.get("nodes", []))
        if data.get("nodes") and ready_nodes == len(data["nodes"]):
            summary += f" All {ready_nodes} nodes are Ready." if ready_nodes != 1 else " The node is Ready."
    if incomplete and not findings:
        summary = "Health is unknown because collection or analysis is incomplete. Open incidents remain tracked."
    return report.model_copy(update={"findings": findings, "coverage": coverage, "summary": summary,
                                     "overall_severity": severity, "recommended_actions": actions,
                                     "recovered_events": recovered_termination_details(data, _termination_detail),
                                     "finding_aliases": aliases})
