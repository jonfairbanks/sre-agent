"""Per-finding notification controls backed by durable report identities."""
from __future__ import annotations

import hashlib
import logging
import re

log = logging.getLogger("sre-agent.finding-mutes")
_ACTION_ID = "sre_ignore_finding"
_REPORT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_VALUE = re.compile(r"v1:([A-Za-z0-9_-]{1,32}):([a-f0-9]{64}):(1h|8h|1d|1w|forever|unignore)\Z")
_PERIODS = {
    "1h": ("1 Hour", 1, False),
    "8h": ("8 Hours", 8, False),
    "1d": ("1 Day", 24, False),
    "1w": ("1 Week", 168, False),
    "forever": ("Forever", None, True),
    "unignore": ("Unignore", 0, False),
}


def _accessory(report_id: str, fingerprint_hash: str) -> dict:
    return {
        "type": "static_select",
        "action_id": _ACTION_ID,
        "placeholder": {"type": "plain_text", "text": "Ignore Finding"},
        "options": [
            {
                "text": {"type": "plain_text", "text": label},
                "value": f"v1:{report_id}:{fingerprint_hash}:{period}",
            }
            for period, (label, _, _) in _PERIODS.items()
        ],
    }


def build_ignore_accessory(report_id: str, fingerprint: str) -> dict:
    """Build a Slack menu whose values stay below Slack's 150-character limit.

    Persistence must resolve the hash only against fingerprints covered by the
    referenced report. Hashing bounds the payload even for long resource names.
    """
    if not isinstance(report_id, str) or not _REPORT_ID.fullmatch(report_id):
        raise ValueError("Invalid report ID")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Missing finding fingerprint")
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return _accessory(report_id, digest)


def _parse_selection(body: dict) -> tuple[str, str, str]:
    actions = body.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise ValueError("Invalid action")
    action = actions[0]
    if not isinstance(action, dict) or action.get("action_id") != _ACTION_ID:
        raise ValueError("Invalid action")
    selected = action.get("selected_option")
    value = selected.get("value") if isinstance(selected, dict) else None
    match = _VALUE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Invalid selection")
    return match.groups()


def register_finding_mute_actions(bolt_app, db, approver_allowed):
    """Install controls using the existing Slack approver policy.

    The database atomically validates report membership and current finding
    state before changing a mute. No state is trusted from the Slack payload.
    """
    @bolt_app.action(_ACTION_ID)
    def handle_finding_mute(ack, body, client):
        ack()
        user = body.get("user") if isinstance(body, dict) else None
        channel = body.get("channel") if isinstance(body, dict) else None
        actor_id = user.get("id") if isinstance(user, dict) else None
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(actor_id, str) or not actor_id:
            return
        if not isinstance(channel_id, str) or not channel_id:
            return

        def reply(text, accessory=None):
            kwargs = {"channel": channel_id, "user": actor_id, "text": text}
            if accessory is not None:
                kwargs["blocks"] = [{
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text},
                    "accessory": accessory,
                }]
            try:
                client.chat_postEphemeral(**kwargs)
            except Exception:
                log.warning("Could not confirm finding mute to the Slack user")

        try:
            allowed = approver_allowed(actor_id)
        except Exception:
            allowed = False
        if not allowed:
            reply("You are not authorized to change finding notifications.")
            return
        try:
            report_id, fingerprint_hash, period = _parse_selection(body)
        except ValueError:
            reply("This finding control is invalid. Use a current report.")
            return
        label, hours, forever = _PERIODS[period]
        try:
            changed = db is not None and db.mute_report_finding(
                report_id, fingerprint_hash, hours, forever=forever,
            )
        except Exception:
            log.warning("Could not update the finding mute")
            reply("Could not save this change. Try again.")
            return
        if not changed:
            reply("This finding is no longer available in this report. Use a current report or the ignored findings list.")
            return
        if period == "unignore":
            text = "Finding unignored. Notifications will resume on the next eligible check."
        elif forever:
            text = "Finding ignored until you choose Unignore. Tracking and resolution checks will continue."
        else:
            text = f"Finding ignored for {label.lower()}. Tracking and resolution checks will continue."
        reply(text, _accessory(report_id, fingerprint_hash))

    return handle_finding_mute


def make_finding_mute_tools(db) -> list:
    """Expose ignored findings for inspection; writes stay in authorized controls."""
    import json
    from langchain.tools import tool

    @tool
    def list_ignored_findings(limit: int = 50) -> str:
        """List current ignored findings and report IDs for notification controls.

        Findings remain tracked while ignored. Use Slack finding controls or the
        authorized API to ignore or unignore a finding.
        """
        if db is None or getattr(db, "available", True) is False:
            return "Finding history is unavailable."
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return "Limit must be an integer between 1 and 100."
        try:
            rows = db.list_muted_findings(limit=limit)
        except Exception:
            log.warning("Could not list ignored findings")
            return "Finding history is unavailable. Try again."
        return json.dumps(rows, default=str)

    return [list_ignored_findings]
