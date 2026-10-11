from health_history import recovered_termination_details
from health_evidence import reconcile_report
from schemas import HealthReport


def pod(name="api", status="Running", ready=True, age=10, reason="Error", exit_code=1):
    return {"name": name, "namespace": "apps", "status": status, "ready": ready,
            "last_termination_records": [{"container": "app", "reason": reason, "exit_code": exit_code,
                "finished_at": "2026-10-11T02:37:15Z", "minutes_ago": age}]}


def test_only_recent_recovered_failures_are_reported_and_bounded(monkeypatch):
    monkeypatch.setenv("POD_FAILURE_RECENCY_MINUTES", "240")
    rows = [pod(), pod("unready", ready=False), pod("active", status="Error"),
            pod("old", age=241), pod("no-clock", age=None), pod("completed", reason="Completed", exit_code=0)]
    assert recovered_termination_details({"pods": rows}, lambda r: r["reason"]) == [
        "apps/api: Running and Ready. Error"]
    details = recovered_termination_details({"pods": [pod(str(i)) for i in range(15)]}, lambda r: "Error")
    assert len(details) == 11 and details[-1] == "5 other pods have recovered termination events."


def test_report_recovery_context_is_factual_serializable_and_not_a_finding(monkeypatch):
    monkeypatch.setenv("ALERT_TIMEZONE", "Europe/London")
    report = HealthReport(overall_severity="ok", summary="Healthy.", recovered_events=["invented"])
    result = reconcile_report(report, {"pods": [pod()]})
    assert not result.findings
    assert "apps/api" in result.recovered_events[0]
    assert "3:37:15 AM BST" in result.recovered_events[0]
    assert "exit" not in result.recovered_events[0].lower()
    assert HealthReport.model_validate_json(result.model_dump_json()).recovered_events == result.recovered_events
    assert reconcile_report(report, {"pods": []}).recovered_events == []
