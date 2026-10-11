"""Snapshot facts own incident identity; model prose adds context."""
from __future__ import annotations

import os

from monitor_state import fingerprint
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


def reconcile_report(report: HealthReport, data: dict, stored: dict | None = None) -> HealthReport:
    """Derive factual findings and preserve independent model observations."""
    stored = stored or {}
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
            f"Current pod evidence: {pod['status']}; last termination: {pod.get('last_termination') or 'none'}.",
            pod.get("owner_kind", ""), pod.get("owner_name", ""))
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
        for key, usage in data.get(area, {}).items():
            namespace, name = (key.split("/", 1) if kind == "PersistentVolumeClaim" else ("", usage.get("node", key)))
            disk_objects.add((kind.lower(), namespace, name))
            candidate = Finding(severity="warning", title="Disk Usage", detail="", kind=kind,
                                resource_name=name, namespace=namespace, reason=reason)
            candidate_fp = fingerprint(candidate)
            previous = stored.get(candidate_fp)
            open_previous = previous is not None and previous.resolved_at is None
            percent = usage.get("percent")
            if not isinstance(percent, (int, float)):
                continue
            disk_observations.add(candidate_fp)
            threshold = DISK_CLEAR_PERCENT if open_previous else DISK_ENTER_PERCENT
            if percent >= threshold:
                severity = "critical" if percent >= DISK_CRITICAL_PERCENT else "warning"
                if open_previous and previous.severity == "critical" and percent >= DISK_CRITICAL_CLEAR_PERCENT:
                    severity = "critical"
                qualifier = " Shared filesystem usage is not PVC data size." if kind == "Node" else ""
                add(kind, name, namespace, reason, severity, f"Filesystem usage is {percent}%.{qualifier}")

    fact_ids = {fingerprint(f) for f in facts}
    fact_resources = {(f.kind.lower(), f.namespace, f.resource_name) for f in facts}
    known_pods = {(p["namespace"], p["name"]) for p in data.get("pods", [])}
    known_hpas = {(h["namespace"], h["name"]): h for h in data.get("hpas", [])}
    pod_reasons = {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
                   "CreateContainerConfigError", "CreateContainerError", "ErrImageNeverPull",
                   "Failed", "Pending", "NotReady", "OOMKilled", "Error", "ContainerCannotRun",
                   "DeadlineExceeded", "Evicted"}
    model = []
    for finding in report.findings:
        if finding.evidence_source == "snapshot":
            continue
        resource = (finding.kind.lower(), finding.namespace, finding.resource_name)
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
    if incomplete and not findings:
        summary = "Health is unknown because collection or analysis is incomplete. Open incidents remain tracked."
    return report.model_copy(update={"findings": findings, "coverage": coverage, "summary": summary,
                                     "overall_severity": severity})
