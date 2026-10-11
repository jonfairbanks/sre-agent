"""Bounded, database-injected evidence for incident follow-up questions."""
from datetime import datetime, timedelta, timezone
import json
import re
from langchain.tools import tool
from config import TOOL_OUTPUT_MAX_CHARS


def _safe_history(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'password|token|secret|credential|private.?key', str(k), re.I)
                else _safe_history(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_history(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'(?i)(bearer\s+|(?:password|token|secret|api[_-]?key)\s*[:=]\s*)[^\s,;]+',
                      r'\1[REDACTED]', value)[:4000]
    return value


def _bounded_history(payload, key):
    payload[key] = list(payload[key])
    omitted = 0
    while True:
        payload['omitted_records'] = omitted
        result = json.dumps(payload, default=str)
        if len(result) <= max(512, TOOL_OUTPUT_MAX_CHARS - 128):
            return result
        if not payload[key]:
            return json.dumps({'error': 'History evidence exceeds output limit', 'health': 'unknown'})
        payload[key].pop()
        omitted += 1


def make_incident_history_tools(db) -> list:
    """Create tools with the API process's database, never a global connection."""
    @tool
    def get_incident_history(namespace: str = '', since: str = '', limit: int = 20) -> str:
        """Read monitor observations and diffs. Since is timezone-aware ISO 8601, default 24 hours. Missing coverage is not recovery."""
        if db is None or getattr(db, 'available', True) is False:
            return json.dumps({'error': 'Durable incident history is unavailable', 'health': 'unknown'})
        try:
            start = datetime.fromisoformat(since.replace('Z', '+00:00')) if since else datetime.now(timezone.utc) - timedelta(days=1)
            if start.tzinfo is None:
                raise ValueError('Since must include a timezone')
            if not 1 <= limit <= 50:
                raise ValueError('Limit must be between 1 and 50')
            if namespace and not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?', namespace):
                raise ValueError('Invalid namespace')
            records = db.recent_monitor_checks(since=start, namespace=namespace, limit=limit)
            return _bounded_history({'since': start.isoformat(), 'namespace': namespace, 'checks': _safe_history(records),
                               'interpretation': 'Observations only. Invalid analyses or missing coverage leave health unknown; recurrence timing does not establish cause.'}, 'checks')
        except ValueError as exc:
            return json.dumps({'error': str(exc)})
        except Exception:
            return json.dumps({'error': 'Incident history unavailable', 'health': 'unknown'})

    @tool
    def get_finding_history(fingerprint: str, limit: int = 20) -> str:
        """Read observations for one finding fingerprint, including recurrence and clearing evidence."""
        if db is None or getattr(db, 'available', True) is False:
            return json.dumps({'error': 'Durable finding history is unavailable', 'health': 'unknown'})
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', fingerprint) or not 1 <= limit <= 50:
            return json.dumps({'error': 'Invalid fingerprint or limit (1 to 50)'})
        try:
            return _bounded_history({'fingerprint': fingerprint, 'observations': _safe_history(db.finding_history(fingerprint, limit=limit)),
                               'interpretation': 'Absence is not recovery unless a valid check covered the resource.'}, 'observations')
        except Exception:
            return json.dumps({'error': 'Finding history unavailable', 'health': 'unknown'})

    return [get_incident_history, get_finding_history]
