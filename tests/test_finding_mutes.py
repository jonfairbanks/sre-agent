"""Finding control validation without a Slack connection."""
import hashlib
from unittest.mock import Mock

import pytest

from finding_mutes import build_ignore_accessory, register_finding_mute_actions


FP = "prod/deployment/api:crashloopbackoff"
REPORT = "abc123"


class FakeBolt:
    def action(self, action_id):
        assert action_id == "sre_ignore_finding"
        def register(handler):
            self.handler = handler
            return handler
        return register


def body_for(period):
    options = build_ignore_accessory(REPORT, FP)["options"]
    option = next(o for o in options if o["value"].endswith(":" + period))
    return {
        "user": {"id": "U123", "name": "untrusted-name"},
        "channel": {"id": "C123"},
        "actions": [{"action_id": "sre_ignore_finding", "selected_option": option}],
    }


def invoke(body, *, allowed=True, changed=True, failure=None):
    bolt, db, client, ack = FakeBolt(), Mock(), Mock(), Mock()
    db.mute_report_finding.return_value = changed
    db.mute_report_finding.side_effect = failure
    authorize = Mock(return_value=allowed)
    register_finding_mute_actions(bolt, db, authorize)
    def assert_ack_before_write(*args, **kwargs):
        ack.assert_called_once_with()
        if failure is not None:
            raise failure
        return changed
    db.mute_report_finding.side_effect = assert_ack_before_write
    bolt.handler(ack, body, client)
    ack.assert_called_once_with()
    return db, client, authorize


def test_accessory_has_bounded_values_and_title_case_labels():
    accessory = build_ignore_accessory("r" * 32, "long-resource-" * 1000)
    assert accessory["type"] == "static_select"
    assert accessory["action_id"] == "sre_ignore_finding"
    assert [o["text"]["text"] for o in accessory["options"]] == [
        "1 Hour", "8 Hours", "1 Day", "1 Week", "Forever", "Unignore",
    ]
    assert all(len(o["value"]) <= 150 for o in accessory["options"])


@pytest.mark.parametrize("report_id", ["", "r" * 33, "bad:id", "r\n", None])
def test_accessory_rejects_invalid_report_ids(report_id):
    with pytest.raises(ValueError):
        build_ignore_accessory(report_id, FP)


@pytest.mark.parametrize("period,hours,forever", [
    ("1h", 1, False), ("8h", 8, False), ("1d", 24, False),
    ("1w", 168, False), ("forever", None, True), ("unignore", 0, False),
])
def test_allowed_periods_update_one_durable_finding(period, hours, forever):
    db, client, authorize = invoke(body_for(period))
    authorize.assert_called_once_with("U123")
    db.mute_report_finding.assert_called_once_with(
        REPORT, hashlib.sha256(FP.encode()).hexdigest(), hours, forever=forever,
    )
    posted = client.chat_postEphemeral.call_args.kwargs
    assert posted["user"] == "U123"
    assert posted["channel"] == "C123"
    assert "untrusted-name" not in posted["text"]
    # Success replies retain Unignore even after an indefinite mute.
    assert posted["blocks"][0]["accessory"]["options"][-1]["text"]["text"] == "Unignore"
    if forever:
        assert "until you choose Unignore" in posted["text"]


@pytest.mark.parametrize("value", [
    "v1:abc123:" + "a" * 64 + ":2h",
    "v1:abc123:" + "a" * 64 + ":-1",
    "v1:abc123:" + "a" * 64 + ":3600",
    "v1:abc123:" + "a" * 64 + ":forever\n",
    "v1:abc123:arbitrary-fingerprint:forever",
    "[\"fingerprint1\", \"fingerprint2\"]", None,
])
def test_tampered_options_never_write(value):
    body = body_for("1h")
    body["actions"][0]["selected_option"]["value"] = value
    db, client, _ = invoke(body)
    db.mute_report_finding.assert_not_called()
    assert "invalid" in client.chat_postEphemeral.call_args.kwargs["text"]


def test_nonmember_hash_fails_closed():
    body = body_for("forever")
    body["actions"][0]["selected_option"]["value"] = "v1:abc123:" + "0" * 64 + ":forever"
    db, client, _ = invoke(body, changed=False)
    db.mute_report_finding.assert_called_once()
    posted = client.chat_postEphemeral.call_args.kwargs
    assert "no longer available" in posted["text"]
    assert "blocks" not in posted


def test_denied_actor_never_writes():
    db, client, _ = invoke(body_for("forever"), allowed=False)
    db.mute_report_finding.assert_not_called()
    assert "not authorized" in client.chat_postEphemeral.call_args.kwargs["text"]


@pytest.mark.parametrize("body", [None, {}, {"user": "malformed"}, {"user": {"id": "U1"}}])
def test_incomplete_callback_is_acknowledged_without_write(body):
    db, client, authorize = invoke(body)
    db.mute_report_finding.assert_not_called()
    client.chat_postEphemeral.assert_not_called()
    authorize.assert_not_called()


def test_multiple_actions_are_rejected():
    body = body_for("1h")
    body["actions"] *= 2
    db, _, _ = invoke(body)
    db.mute_report_finding.assert_not_called()


def test_storage_error_does_not_expose_payload_or_secret(caplog):
    body = body_for("1h")
    body["token"] = "private-slack-token"
    db, client, _ = invoke(body, failure=RuntimeError("private-database-token"))
    assert "Try again" in client.chat_postEphemeral.call_args.kwargs["text"]
    assert "private" not in caplog.text
    assert "private" not in str(client.chat_postEphemeral.call_args)


def test_authorizer_error_fails_closed():
    bolt, db, client, ack = FakeBolt(), Mock(), Mock(), Mock()
    register_finding_mute_actions(bolt, db, Mock(side_effect=RuntimeError("private-token")))
    bolt.handler(ack, body_for("forever"), client)
    db.mute_report_finding.assert_not_called()
    assert "not authorized" in client.chat_postEphemeral.call_args.kwargs["text"]


def test_read_only_tool_lists_safe_database_rows():
    from finding_mutes import make_finding_mute_tools
    db = Mock()
    db.list_muted_findings.return_value = [{"title": "Pod Failure", "report_id": REPORT}]
    tools = make_finding_mute_tools(db)
    assert len(tools) == 1
    assert tools[0].name == "list_ignored_findings"
    assert "Pod Failure" in tools[0].invoke({"limit": 10})
    db.list_muted_findings.assert_called_once_with(limit=10)
    db.mute_report_finding.assert_not_called()


@pytest.mark.parametrize("limit", [0, 101, -1, True, "50"])
def test_read_only_tool_rejects_invalid_limits(limit):
    from finding_mutes import make_finding_mute_tools
    db = Mock()
    result = make_finding_mute_tools(db)[0].func(limit)
    assert "between 1 and 100" in result
    db.list_muted_findings.assert_not_called()


def test_read_only_tool_hides_database_error(caplog):
    from finding_mutes import make_finding_mute_tools
    db = Mock()
    db.list_muted_findings.side_effect = RuntimeError("private-token")
    result = make_finding_mute_tools(db)[0].invoke({})
    assert "unavailable" in result
    assert "private-token" not in caplog.text


def test_unavailable_database_does_not_claim_no_ignored_findings():
    from persistence import NullDatabase
    from finding_mutes import make_finding_mute_tools
    tool = make_finding_mute_tools(NullDatabase())[0]
    assert "unavailable" in tool.invoke({"limit": 50}).lower()
