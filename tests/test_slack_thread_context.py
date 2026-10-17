"""Slack follow-ups receive bounded thread evidence without replaying checkpoints."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import api


@pytest.fixture
def thread(monkeypatch):
    agent = Mock()
    agent.get_state.return_value = SimpleNamespace(values={"messages": []})
    monkeypatch.setattr(api, "_agent", agent)
    monkeypatch.setattr(api, "_db", None)
    client = Mock()
    client.conversations_replies.return_value = {"messages": []}
    client.chat_postMessage.return_value = {"ts": "9.000001"}
    session = api.Session("slack-1", "slack-1", source="slack")
    return agent, client, session


def hydrate(thread, event_ts="4.000001"):
    _, client, session = thread
    return api._slack_agent_input("check that pod", session, client, "C1", "1.000001", event_ts)


def test_parent_report_attachment_and_preceding_human_reply_are_available(thread):
    _, client, _ = thread
    client.conversations_replies.return_value = {"messages": [
        {"ts": "1.000001", "bot_id": "B1", "text": "Issues Found",
         "blocks": [{"type": "header", "text": {"type": "plain_text", "text": "Cluster Health"}}],
         "attachments": [{"blocks": [{"type": "section", "text": {
             "type": "mrkdwn", "text": "Pod `prod/api-123` is not ready."},
             "accessory": {"type": "button", "text": {"text": "Ignore"}, "value": "private-action"}}]}]},
        {"ts": "2.000001", "user": "U1", "blocks": [{"type": "rich_text", "elements": [
            {"type": "rich_text_section", "elements": [{"type": "text", "text": "Use the prod namespace"}]}]}]},
        {"ts": "3.000001", "bot_id": "B1", "text": ":hourglass_flowing_sand: Working on it..."},
        {"ts": "4.000001", "text": "current should not repeat"},
        {"ts": "5.000001", "text": "future must not leak"},
    ]}
    message = hydrate(thread)
    content = message["content"]
    assert "prod/api-123" in content and "Use the prod namespace" in content
    assert "Cluster Health" in content
    assert "Working on it" not in content
    assert "current should not repeat" not in content and "future must not leak" not in content
    assert "private-action" not in content and "Ignore" not in content
    assert content.endswith("Current request:\ncheck that pod")
    assert message["additional_kwargs"] == {"slack_event_ts": "4.000001"}
    client.conversations_replies.assert_called_once_with(
        channel="C1", ts="1.000001", latest="4.000001", inclusive=False, limit=100)


def test_checkpoint_watermark_skips_prior_context_and_model_reply(thread):
    agent, client, _ = thread
    agent.get_state.return_value = SimpleNamespace(values={"messages": [
        HumanMessage(content="prior request", additional_kwargs={"slack_event_ts": "2.000001"}),
        AIMessage(content="The pod is prod/api-123."),
    ]})
    client.conversations_replies.return_value = {"messages": [
        {"ts": "1.000001", "text": "old report"},
        {"ts": "2.000001", "text": "prior request"},
        {"ts": "3.000001", "bot_id": "B1", "text": "The pod is prod/api-123."},
        {"ts": "3.000002", "user": "U2", "text": "Look at the previous container too"},
    ]}
    content = hydrate(thread)["content"]
    assert "old report" not in content and "prior request" not in content
    assert "The pod is" not in content
    assert "previous container" in content
    assert client.conversations_replies.call_args.kwargs["oldest"] == "2.000001"


def test_pagination_keeps_chronological_context(thread):
    _, client, _ = thread
    client.conversations_replies.side_effect = [
        {"messages": [{"ts": "1.000001", "text": "parent"}],
         "has_more": True, "response_metadata": {"next_cursor": "next"}},
        {"messages": [{"ts": "2.000001", "text": "reply"}]},
    ]
    content = hydrate(thread)["content"]
    assert content.index("parent") < content.index("reply")
    assert client.conversations_replies.call_args.kwargs["cursor"] == "next"


def test_bounded_history_warns_about_missing_referents(thread, monkeypatch):
    _, client, _ = thread
    monkeypatch.setattr(api, "_SLACK_THREAD_CHARS", 100)
    client.conversations_replies.return_value = {
        "messages": [{"ts": "1.000001", "text": "parent"},
                     {"ts": "2.000001", "text": "x" * 500}],
        "has_more": True, "response_metadata": {"next_cursor": "next"},
    }
    content = hydrate(thread)["content"]
    assert client.conversations_replies.call_count == api._SLACK_THREAD_PAGES
    assert "parent" in content
    assert "Thread history is incomplete" in content and "do not infer it" in content
    assert "x" * 500 not in content


@pytest.mark.parametrize("failure", ["slack", "checkpoint", "api_error"])
def test_fetch_failure_does_not_run_context_free_agent(thread, failure, caplog):
    agent, client, session = thread
    if failure == "slack":
        client.conversations_replies.side_effect = RuntimeError("private-token")
    elif failure == "checkpoint":
        agent.get_state.side_effect = RuntimeError("private-token")
    else:
        client.conversations_replies.return_value = {"ok": False, "error": "missing_scope"}
    api._run_for_slack("check that pod", session, client, "C1", "1.000001", "4.000001")
    agent.invoke.assert_not_called()
    assert session.status == api.SessionStatus.ERROR
    assert "Please restate the resource" in client.chat_update.call_args.kwargs["text"]
    assert "private-token" not in caplog.text


def test_new_top_level_message_needs_no_history_and_invoke_retains_metadata(thread):
    agent, client, session = thread
    agent.invoke.return_value = {"messages": [AIMessage(content="done")]}
    api._run_for_slack("list pods", session, client, "C1", "1.000001", "1.000001")
    agent.get_state.assert_not_called()
    client.conversations_replies.assert_not_called()
    assert agent.invoke.call_args.args[0]["messages"] == [{
        "role": "user", "content": "list pods", "additional_kwargs": {"slack_event_ts": "1.000001"}}]
    assert session.status == api.SessionStatus.DONE


def test_oversized_parent_keeps_resource_evidence(thread, monkeypatch):
    _, client, _ = thread
    monkeypatch.setattr(api, "_SLACK_THREAD_CHARS", 100)
    client.conversations_replies.return_value = {"messages": [
        {"ts": "1.000001", "text": "Pod prod/api-123 failed. " + "x" * 500}]}
    content = hydrate(thread)["content"]
    assert "Pod prod/api-123 failed" in content
    assert "Thread history is incomplete" in content
    assert "x" * 500 not in content


def test_rich_text_link_without_label_keeps_url():
    assert api._slack_message_text({"blocks": [{"type": "rich_text", "elements": [
        {"type": "link", "url": "https://example.com/pod"}]}]}) == "https://example.com/pod"


def test_uploaded_response_summary_is_not_duplicated(thread):
    agent, client, _ = thread
    agent.get_state.return_value = SimpleNamespace(values={"messages": [
        HumanMessage(content="prior", additional_kwargs={"slack_event_ts": "2.000001"}),
        AIMessage(content="Pod Status\n" + "x" * 4000),
    ]})
    client.conversations_replies.return_value = {"messages": [{"ts": "3.000001", "bot_id": "B1",
        "text": "Pod Status\n\n_Full output uploaded as a file below._"}]}
    assert hydrate(thread)["content"] == "check that pod"


@pytest.mark.parametrize("reply", [False, True])
@pytest.mark.parametrize("previous_error", [False, True])
def test_only_top_level_health_check_uses_cluster_wide_shortcut(thread, monkeypatch, reply, previous_error):
    import slack_bolt
    import slack_bolt.adapter.socket_mode
    import threading
    agent, client, _ = thread
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-test")
    monkeypatch.setattr(api, "_slack_shutdown", threading.Event())
    monkeypatch.setattr(api, "_sessions", {})
    monkeypatch.setattr(api, "_executor", Mock())
    monkeypatch.setattr(api, "_notifier", None)
    callbacks = {}

    class Bolt:
        def __init__(self, **kwargs): pass
        def event(self, name):
            def register(fn):
                callbacks[name] = fn
                return fn
            return register
        def action(self, name): return lambda fn: fn

    class Handler:
        def __init__(self, *args, **kwargs):
            self.client = SimpleNamespace(on_message_listeners=[], on_error_listeners=[],
                on_close_listeners=[], is_connected=lambda: False)
        def connect(self): pass

    monkeypatch.setattr(slack_bolt, "App", Bolt)
    monkeypatch.setattr(slack_bolt.adapter.socket_mode, "SocketModeHandler", Handler)
    api._start_slack_bolt(None)
    event = {"channel": "C1", "ts": "4.000001", "text": "<@B1> run a health check on this"}
    if reply:
        event["thread_ts"] = "1.000001"
    if previous_error:
        session_id = f"slack-{event.get('thread_ts', event['ts'])}"
        api._sessions[session_id] = api.Session(session_id, session_id, status=api.SessionStatus.ERROR)
    callbacks["app_mention"](event, {}, client)
    submitted = api._executor.submit.call_args.args
    assert submitted[0] is (api._run_for_slack if reply else api._run_structured_health_check_for_slack)
    if reply:
        assert submitted[-1] == event["ts"]
    assert api._sessions[f"slack-{event.get('thread_ts', event['ts'])}"].status == api.SessionStatus.RUNNING


@pytest.mark.parametrize("worker", [api._run_for_slack, api._run_structured_health_check_for_slack])
def test_initial_slack_post_failure_releases_running_session(thread, worker, monkeypatch, caplog):
    agent, client, session = thread
    session.status = api.SessionStatus.RUNNING
    database = Mock()
    monkeypatch.setattr(api, "_db", database)
    client.chat_postMessage.side_effect = RuntimeError("private-token")
    worker("check", session, client, "C1", "1.000001")
    assert session.status == api.SessionStatus.ERROR
    assert database.save_session.call_args.args[0]["status"] == "error"
    agent.invoke.assert_not_called()
    client.chat_update.assert_not_called()
    assert "RuntimeError" in caplog.text and "private-token" not in caplog.text

    # A transient delivery failure leaves the next accepted request runnable.
    import scheduler
    from schemas import HealthReport
    monkeypatch.setattr(api, "_db", None)
    monkeypatch.setattr(api, "_scheduler", None)
    monkeypatch.setattr(api, "_notifier", None)
    monkeypatch.setattr(scheduler, "run_structured_health_check", lambda: (
        HealthReport(overall_severity="ok", summary="Healthy"), {}))
    client.chat_postMessage.side_effect = None
    agent.invoke.return_value = {"messages": [AIMessage(content="done")]}
    session.status = api.SessionStatus.RUNNING
    worker("check", session, client, "C1", "1.000001")
    assert session.status == api.SessionStatus.DONE


def test_unavailable_agent_releases_running_session(thread, monkeypatch, caplog):
    _, client, session = thread
    session.status = api.SessionStatus.RUNNING
    database = Mock()
    monkeypatch.setattr(api, "_db", database)
    monkeypatch.setattr(api, "_agent", None)
    api._run_for_slack("check", session, client, "C1", "1.000001")
    assert session.status == api.SessionStatus.ERROR
    assert database.save_session.call_args.args[0]["status"] == "error"
    client.chat_postMessage.assert_not_called()
    assert "Slack agent failed (RuntimeError)" in caplog.text
