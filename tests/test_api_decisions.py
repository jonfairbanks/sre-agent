"""HTTP approval decisions preserve action counts and the middleware contract."""
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from langchain.agents.middleware.human_in_the_loop import EditDecision
from pydantic import TypeAdapter
import pytest
import api


@pytest.fixture
def state(monkeypatch):
    db = Mock()
    db.load_session.return_value = None
    loop = Mock()
    agent = Mock()
    monkeypatch.setattr(api, "_sessions", {})
    monkeypatch.setattr(api, "_db", db)
    monkeypatch.setattr(api, "_notifier", None)
    monkeypatch.setattr(api, "_agent", agent)
    monkeypatch.setattr(api, "asyncio", SimpleNamespace(get_event_loop=lambda: loop))
    return TestClient(api.app), db, loop, agent


def pending():
    return api.Session(id="review", thread_id="thread", status=api.SessionStatus.INTERRUPTED,
                       pending_decisions=2, pending_actions=[
                           {"name": "kubectl_scale", "args": {"replicas": 2}},
                           {"name": "kubectl_restart", "args": {"name": "worker"}},
                       ])


@pytest.mark.parametrize("route", ["approve", "reject", "edit"])
@pytest.mark.parametrize("status,expected", [(None, 404), (api.SessionStatus.RUNNING, 409)])
def test_invalid_session_or_state_never_audits_or_dispatches(state, route, status, expected):
    client, db, loop, agent = state
    if status:
        session = pending()
        session.status = status
        api._sessions[session.id] = session
    response = client.post(f"/api/{route}", json={"session_id": "review", "args": {"replicas": 3}})
    assert response.status_code == expected
    db.record_decision.assert_not_called()
    loop.run_in_executor.assert_not_called()
    agent.invoke.assert_not_called()


@pytest.mark.parametrize("route", ["approve", "reject"])
def test_valid_decision_audits_each_action_and_dispatches_matching_batch(state, route):
    client, db, loop, agent = state
    session = pending()
    api._sessions[session.id] = session
    response = client.post(f"/api/{route}", json={"session_id": session.id, "reason": "Keep current state"})
    assert response.status_code == 200
    assert response.json() == {"session_id": session.id, "status": "running"}
    rows = [call.kwargs for call in db.record_decision.call_args_list]
    assert [row["tool_name"] for row in rows] == [action["name"] for action in session.pending_actions]
    assert all(row["decision"] == route and row["actor"] == "api-user" for row in rows)
    dispatch = loop.run_in_executor.call_args.args
    assert dispatch[1] is api._resume_agent_sync and dispatch[2] is agent
    expected = {"type": route}
    if route == "reject":
        expected["message"] = "Keep current state"
    assert dispatch[3].resume == {"decisions": [expected, expected]}
    assert session.status == api.SessionStatus.RUNNING
    agent.invoke.assert_not_called()


def test_edit_uses_named_edited_action_contract(state):
    client, db, loop, agent = state
    session = pending()
    session.pending_actions = session.pending_actions[:1]
    session.pending_decisions = 1
    api._sessions[session.id] = session
    response = client.post("/api/edit", json={"session_id": session.id, "args": {"replicas": 3}})
    assert response.status_code == 200
    decision = loop.run_in_executor.call_args.args[3].resume["decisions"][0]
    TypeAdapter(EditDecision).validate_python(decision)
    assert decision == {"type": "edit", "edited_action": {"name": "kubectl_scale", "args": {"replicas": 3}}}
    db.record_decision.assert_called_once()
    agent.invoke.assert_not_called()


def test_pending_decision_count_uses_actions_within_one_interrupt(state, monkeypatch):
    client, db, loop, agent = state
    session = pending()
    actions = session.pending_actions
    def publish(coroutine, event_loop):
        coroutine.close()
    monkeypatch.setattr(api.asyncio, "run_coroutine_threadsafe", publish, raising=False)
    api._handle_result({"__interrupt__": [SimpleNamespace(value={"action_requests": actions})]}, session, loop)
    assert session.pending_decisions == 2
    assert session.pending_actions == actions
    assert session.status == api.SessionStatus.INTERRUPTED


def test_edit_without_arguments_never_audits_or_dispatches(state):
    client, db, loop, agent = state
    session = pending()
    api._sessions[session.id] = session
    assert client.post("/api/edit", json={"session_id": session.id}).status_code == 422
    db.record_decision.assert_not_called()
    loop.run_in_executor.assert_not_called()
    agent.invoke.assert_not_called()


@pytest.mark.parametrize("actions,count", [([], 1), ([{"name": "scale"}, {"name": "restart"}], 2), ([{}], 1)])
def test_ambiguous_edit_never_audits_or_dispatches(state, actions, count):
    client, db, loop, agent = state
    session = pending()
    session.pending_actions = actions
    session.pending_decisions = count
    api._sessions[session.id] = session
    assert client.post("/api/edit", json={"session_id": session.id, "args": {}}).status_code == 409
    db.record_decision.assert_not_called()
    loop.run_in_executor.assert_not_called()
    assert session.status == api.SessionStatus.INTERRUPTED
