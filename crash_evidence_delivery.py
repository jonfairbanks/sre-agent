"""Retry incomplete crash captures without posting another alert."""
from copy import deepcopy
from datetime import datetime

from crash_watcher import collect_crash_evidence, evidence_core_v1


def enrich_crash_payload(payload, collector=None):
    """Preserve captured sections when a later source read fails."""
    updated = deepcopy(payload)
    state = updated["crash_evidence"]
    if state.get("capture_complete"):
        return updated, False
    evidence = (collector or collect_crash_evidence)(
        evidence_core_v1,
        uid=state["uid"], namespace=state["namespace"],
        pod_name=state["pod_name"], container=state["container"],
        now=datetime.fromisoformat(state["now"]),
        expected_finished_at=state.get("expected_finished_at"),
    )
    sections = state["sections"]
    for name in ("termination", "logs", "events"):
        if name == "termination" and sections.get(name):
            continue
        if name == "events" and "\n" in sections.get(name, ""):
            previous_lines = sections[name].splitlines()[1:]
            current_lines = evidence.sections.get(name, "").splitlines()[1:]
            lines = list(dict.fromkeys(previous_lines + current_lines))[:3]
            sections[name] = "Recent Warning Events:\n" + "\n".join(lines)
            continue
        available = name == "termination" or getattr(evidence, f"{name}_available", False)
        if available and evidence.sections.get(name):
            sections[name] = evidence.sections[name]
        elif not state.get(f"{name}_available") and evidence.sections.get(name):
            sections[name] = evidence.sections[name]
    for name in ("logs", "events"):
        state[f"{name}_available"] = bool(
            state.get(f"{name}_available") or getattr(evidence, f"{name}_available", False))
    state["expected_finished_at"] = (state.get("expected_finished_at")
                                     or evidence.expected_finished_at)
    state["capture_count"] = state.get("capture_count", 0) + 1
    pending = evidence.retryable and not (state["logs_available"] and state["events_available"])
    state["capture_complete"] = not pending
    updated["message"] = "\n".join(
        [state["summary"]] + [sections[name] for name in ("termination", "logs", "events")
                              if sections.get(name)])[:2499]
    return updated, pending


def evidence_retry_delay(payload):
    return min(15 * 2 ** min(payload["crash_evidence"].get("capture_count", 0), 5), 300)
