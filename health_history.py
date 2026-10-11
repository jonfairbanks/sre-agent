"""Bounded, factual recovery context kept separate from active findings."""
import os


def recovered_termination_details(data: dict, format_record) -> list[str]:
    lookback = int(os.getenv("POD_FAILURE_RECENCY_MINUTES", "240"))
    failure_reasons = {"OOMKilled", "Error", "ContainerCannotRun", "DeadlineExceeded", "Evicted"}
    details = []
    for pod in sorted(data.get("pods", []), key=lambda p: (p.get("namespace", ""), p.get("name", ""))):
        if pod.get("status") != "Running" or pod.get("ready") is not True:
            continue
        records = pod.get("last_termination_records") or []
        recent = [r for r in records if isinstance(r.get("minutes_ago"), (int, float))
                  and 0 <= r["minutes_ago"] <= lookback
                  and (r.get("reason") in failure_reasons
                       or (r.get("reason") != "Completed" and (r.get("exit_code") or 0) != 0))]
        if not recent:
            continue
        description = " ".join(format_record(record) for record in recent[:4])
        if len(recent) > 4:
            description += f" {len(recent) - 4} other containers recovered."
        details.append(f"{pod['namespace']}/{pod['name']}: Running and Ready. {description}")
    if len(details) > 10:
        return details[:10] + [f"{len(details) - 10} other pods have recovered termination events."]
    return details
