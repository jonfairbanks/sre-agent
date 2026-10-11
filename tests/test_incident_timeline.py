"""Incident chronology preserves timestamps, coverage, and evidence boundaries."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import json

from tools.timeline import build_incident_timeline, make_incident_timeline_tools

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def obj(**kwargs):
    return NS(**kwargs)


def metadata(name, namespace="prod", uid="uid"):
    return obj(name=name, namespace=namespace, uid=uid, owner_references=[])


def event(reason="BackOff", *, ts=NOW - timedelta(hours=1), series=None, kind="Pod", name="api", namespace="prod"):
    return obj(involved_object=obj(kind=kind, name=name, namespace=namespace, uid="uid"),
               reason=reason, type="Warning", count=5, series=series, last_timestamp=ts,
               event_time=NOW - timedelta(hours=4), first_timestamp=NOW - timedelta(hours=5),
               message="password=event-secret", metadata=metadata("evt"))


def pod(name="api", phase="Running", node="worker", ready=True, namespace="prod", restarted=False):
    return obj(metadata=metadata(name, namespace), spec=obj(node_name=node),
               status=obj(phase=phase, conditions=[obj(type="Ready", status="True" if ready else "False")],
                          container_statuses=[obj(name="app", ready=ready, restart_count=2,
                                                  last_state=obj(terminated=obj(finished_at=NOW - timedelta(minutes=30), reason="Error", exit_code=1) if restarted else None))],
                          init_container_statuses=[]))


def node(ready="True"):
    return obj(metadata=metadata("worker", ""), status=obj(conditions=[
        obj(type="Ready", status=ready, reason="KubeletReady", last_transition_time=NOW - timedelta(days=10))]))


class Core:
    def __init__(self, events=None, pods=None, nodes=None, fail_events=False, continued=False):
        self.events, self.pods, self.nodes = events or [], pods or [], nodes or []
        self.fail_events, self.continued = fail_events, continued
        self.calls = []

    def response(self, method, items, **kwargs):
        self.calls.append((method, kwargs))
        return obj(items=items, metadata=obj(_continue="next" if self.continued else ""))

    def list_pod_for_all_namespaces(self, **kwargs):
        return self.response("pods", self.pods, **kwargs)

    def list_namespaced_pod(self, namespace, **kwargs):
        return self.response("pods", self.pods, **kwargs)

    def list_node(self, **kwargs):
        return self.response("nodes", self.nodes, **kwargs)

    def list_event_for_all_namespaces(self, **kwargs):
        if self.fail_events:
            raise RuntimeError("Bearer secret-token password=secret")
        return self.response("events", self.events, **kwargs)

    def list_namespaced_event(self, namespace, **kwargs):
        return self.list_event_for_all_namespaces(**kwargs)


class Custom:
    def __init__(self, apps=None, vault=None, fail=False):
        self.apps, self.vault, self.fail = apps or [], vault or {}, fail
        self.calls = []

    def list_cluster_custom_object(self, group, version, plural, **kwargs):
        self.calls.append((group, version, plural, kwargs))
        if self.fail:
            raise RuntimeError("secret=custom-secret")
        return {"items": self.apps if plural == "applications" else self.vault.get(plural, []), "metadata": {}}

    def list_namespaced_custom_object(self, group, version, namespace, plural, **kwargs):
        return self.list_cluster_custom_object(group, version, plural, **kwargs)


class DB:
    def __init__(self, rows=None, history=None):
        self.rows, self.history = rows or [], history or []
        self.calls = []

    def recent_monitor_checks(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows

    def finding_history(self, fingerprint, limit=20):
        return self.history


def run(core=None, custom=None, db=None, **kwargs):
    return build_incident_timeline(db, now=NOW, core_api=core or Core(), custom_api=custom or Custom(), **kwargs)


def test_event_series_time_precedes_last_timestamp_and_deduplicates():
    item = event(series=obj(last_observed_time=NOW - timedelta(minutes=10), count=8))
    result = run(Core(events=[item, item, event(ts=NOW - timedelta(days=2))]))
    observations = result["observations"]
    assert len(observations) == 1
    assert observations[0]["observed_at"] == (NOW - timedelta(minutes=10)).isoformat()
    assert observations[0]["details"]["count"] == 8


def test_event_time_fallback_and_chronology():
    earlier = event("Scheduled", ts=None)
    later = event("BackOff", ts=NOW - timedelta(minutes=5))
    result = run(Core(events=[later, earlier]))
    assert [row["reason"] for row in result["observations"]] == ["Scheduled", "BackOff"]
    assert result["observations"][0]["observed_at"] == (NOW - timedelta(hours=4)).isoformat()


def test_source_failure_preserves_other_evidence_without_error_secrets():
    result = run(Core(pods=[pod(restarted=True)], fail_events=True), Custom(fail=True))
    assert any(row["source"] == "pod_restarts" for row in result["observations"])
    assert next(c for c in result["coverage"] if c["source"] == "kubernetes_events")["status"] == "unavailable"
    assert "secret-token" not in json.dumps(result)
    assert "custom-secret" not in json.dumps(result)


def test_node_snapshot_lists_cross_resource_impacts_and_old_transition():
    result = run(Core(pods=[pod(), pod("other", namespace="other")], nodes=[node("False")]))
    observation = next(row for row in result["observations"] if row["source"] == "node_conditions")
    assert observation["observed_at"] == NOW.isoformat()
    assert observation["details"]["last_transition_at"] == (NOW - timedelta(days=10)).isoformat()
    assert {ref["namespace"] for ref in observation["details"]["resident_pods"]} == {"prod", "other"}
    assert observation["classification"] == "evidence"
    assert "cause" not in observation


def test_historical_warning_does_not_claim_active_failure_or_recovery():
    result = run(Core(events=[event()], pods=[pod(phase="Succeeded", ready=False)]))
    observation = result["observations"][0]
    assert observation["state"] == "historical"
    assert observation["details"]["current_resource_state"]["phase"] == "Succeeded"
    assert "does not prove" in observation["details"]["state_note"]


def test_monitor_resolutions_are_explicit_and_invalid_analysis_cannot_resolve():
    finding = {"namespace": "prod", "kind": "Pod", "resource_name": "api", "reason": "BackOff", "severity": "warning", "detail": "password=stored-secret"}
    rows = [{"observed_at": NOW - timedelta(hours=2), "analysis_valid": True, "session_id": "check", "check_no": 1,
             "coverage": [{"area": "pods", "status": "complete", "detail": "token=coverage-secret"}],
             "report": {"overall_severity": "warning", "findings": [finding]}, "diff": {}},
            {"observed_at": NOW - timedelta(hours=1), "analysis_valid": False, "report": {}, "diff": {"resolved": [finding]}},
            {"observed_at": NOW, "analysis_valid": True, "report": {}, "diff": {"resolved": [finding]}}]
    result = run(db=DB(rows))
    assert len([o for o in result["observations"] if o["state"] == "resolved"]) == 1
    assert "stored-secret" not in json.dumps(result)
    assert "coverage-secret" not in json.dumps(result)


def test_bounds_page_coverage_and_result_cap():
    core = Core(events=[event(str(i), ts=NOW - timedelta(minutes=i)) for i in range(5)], continued=True)
    custom, db = Custom(), DB()
    result = run(core, custom, db, hours=999, limit=2)
    assert result["window"]["hours"] == 168
    assert result["truncated"] and result["omitted_observations"] == 3
    assert len(result["observations"]) == 2
    assert all(kwargs["limit"] == 200 and kwargs["_request_timeout"] == 15 for _, kwargs in core.calls)
    assert next(c for c in result["coverage"] if c["source"] == "kubernetes_events")["status"] == "partial"
    assert db.calls[0]["limit"] == 50


def test_vault_conditions_are_typed_and_generation_aware():
    vault = {"vaultstaticsecrets": [{"kind": "VaultStaticSecret", "metadata": {"name": "config", "namespace": "prod", "generation": 3},
                                    "spec": {"secret": "DO-NOT-EXPORT"}, "status": {"conditions": [
                                        {"type": "Ready", "status": "False", "reason": "OldFailure", "observedGeneration": 2, "lastTransitionTime": "2026-10-01T00:00:00Z", "message": "DO-NOT-EXPORT"},
                                        {"type": "Healthy", "status": "False", "observedGeneration": 3},
                                        {"type": "Ready", "status": "True"}]}}]}
    result = run(custom=Custom(vault=vault))
    observations = result["observations"]
    assert {o["details"]["freshness"] for o in observations} == {"stale", "current", "unknown"}
    assert {o["details"]["type"] for o in observations} == {"Ready", "Healthy"}
    assert "DO-NOT-EXPORT" not in json.dumps(result)
    assert all(o["state"] == "current" for o in observations)


def test_argo_rollout_joins_namespace_without_exporting_operation_bodies():
    app = {"kind": "Application", "metadata": {"name": "api", "namespace": "argocd"},
           "spec": {"destination": {"namespace": "prod"}, "source": {"repoURL": "https://github.com/example/repo", "helm": {"parameters": [{"value": "DO-NOT-EXPORT"}]}}},
           "status": {"history": [{"deployedAt": (NOW - timedelta(hours=1)).isoformat(), "revision": "abc123"}],
                      "operationState": {"finishedAt": NOW.isoformat(), "phase": "Succeeded", "message": "DO-NOT-EXPORT", "syncResult": {"revision": "abc123"}}}}
    result = run(custom=Custom(apps=[app]), namespace="prod")
    assert len(result["observations"]) == 3
    assert result["observations"][0]["links"][0]["url"] == "https://github.com/example/repo"
    assert "DO-NOT-EXPORT" not in json.dumps(result)


def test_supplied_maintenance_is_unverified_and_redacted_without_causes():
    result = run(maintenance_context="Vault restart token=abcdefgh Bearer xyzsecret")
    observation = result["observations"][0]
    assert observation["source"] == "operator_context"
    assert observation["details"]["verified"] is False
    assert "abcdefgh" not in json.dumps(result)
    assert "xyzsecret" not in json.dumps(result)
    assert "do not establish causes" in result["interpretation"]


def test_finding_history_status_and_namespace_match():
    history = [{"observed_at": NOW, "status": "resolved", "kind": "Pod", "namespace": "prod", "resource_name": "api", "reason": "BackOff", "severity": "warning"},
               {"observed_at": NOW, "status": "new", "kind": "Pod", "namespace": "other", "resource_name": "api", "reason": "BackOff"}]
    result = run(db=DB(history=history), fingerprint="fp", namespace="prod")
    assert len(result["observations"]) == 1
    assert result["observations"][0]["state"] == "resolved"


def test_factory_exposes_read_only_tool():
    tools = make_incident_timeline_tools(DB())
    assert [tool.name for tool in tools] == ["incident_timeline"]


def test_event_does_not_join_current_pod_recreated_with_same_name():
    old_event = event()
    old_event.involved_object.uid = "old-uid"
    result = run(Core(events=[old_event], pods=[pod()]))
    assert result["observations"][0]["details"]["current_resource_state"] is None


def test_stored_check_cap_is_reported_as_partial():
    result = run(db=DB([{ "observed_at": NOW, "report": {} } for _ in range(50)]))
    assert next(c for c in result["coverage"] if c["source"] == "monitor_checks")["status"] == "partial"


def test_serialized_timeline_drops_oldest_without_cutting_json(monkeypatch):
    from tools import timeline
    result = run(Core(events=[event(str(i), ts=NOW - timedelta(minutes=i)) for i in range(40)]))
    monkeypatch.setattr(timeline, "TOOL_OUTPUT_MAX_CHARS", 5000)
    encoded = timeline._bounded_result(result)
    bounded = json.loads(encoded)
    assert len(encoded) < 5000
    assert bounded["truncated"] is True
    assert bounded["omitted_observations"] == 40 - len(bounded["observations"])
    assert bounded["observations"][-1]["reason"] == "0"
    assert bounded["observations"][0]["observed_at"] > result["observations"][0]["observed_at"]
    assert any(c["source"] == "timeline_output" and c["status"] == "partial" for c in bounded["coverage"])
    assert len(result["observations"]) == 40


def test_output_cap_preserves_failure_coverage_and_reports_omitted_metadata(monkeypatch):
    from tools import timeline
    result = run(Core(fail_events=True))
    monkeypatch.setattr(timeline, "TOOL_OUTPUT_MAX_CHARS", 800)
    bounded = json.loads(timeline._bounded_result(result))
    assert bounded["coverage_truncated"] is True
    assert bounded["omitted_coverage"] > 0
    assert any(c["source"] == "kubernetes_events" and c["status"] == "unavailable" for c in bounded["coverage"])


def test_tiny_output_budget_returns_complete_json_with_counts_when_possible(monkeypatch):
    from tools import timeline
    monkeypatch.setattr(timeline, "TOOL_OUTPUT_MAX_CHARS", 256)
    encoded = timeline._bounded_result(run(Core(events=[event()])))
    bounded = json.loads(encoded)
    assert len(encoded) < 256
    assert bounded["omitted_observations"] == 1
    assert bounded["coverage_truncated"] is True


def test_unavailable_database_is_not_reported_as_empty_complete_history():
    result = run(db=obj(available=False), fingerprint="fp")
    history = [c for c in result["coverage"] if c["source"] in ("monitor_checks", "finding_history")]
    assert len(history) == 2
    assert all(c["status"] == "unavailable" for c in history)
