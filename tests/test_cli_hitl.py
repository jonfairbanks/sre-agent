"""CLI approval decisions match each interrupt's requested actions."""
from types import SimpleNamespace
from unittest.mock import Mock

import main


def test_routes_multiple_actions_and_interrupts(monkeypatch):
    actions = [{"name": "scale", "args": {"replicas": 2}}, {"name": "restart", "args": {}}]
    interrupts = [
        SimpleNamespace(id="first", value={"action_requests": actions}),
        SimpleNamespace(id="second", value={"action_requests": [actions[1]]}),
    ]
    review = Mock(side_effect=[{"type": "approve"}, {"type": "reject", "message": "no"}, {"type": "approve"}])
    monkeypatch.setattr(main, "handle_interrupt", review)
    agent = Mock()
    agent.invoke.side_effect = [{"__interrupt__": interrupts}, {"messages": []}]
    main.run_with_hitl(agent, [], {})
    assert review.call_count == 3
    assert agent.invoke.call_args.args[0].resume == {
        "first": {"decisions": [{"type": "approve"}, {"type": "reject", "message": "no"}]},
        "second": {"decisions": [{"type": "approve"}]},
    }


def test_edit_preserves_other_args_and_uses_action_schema(monkeypatch):
    monkeypatch.setattr(main.Prompt, "ask", lambda *args, **kwargs: "e")
    lines = iter(["replicas=3", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(lines))
    assert main.handle_interrupt({"name": "scale", "args": {"namespace": "prod", "replicas": 2}}) == {
        "type": "edit", "edited_action": {"name": "scale", "args": {"namespace": "prod", "replicas": "3"}},
    }


def test_cancel_does_not_resume_partially_reviewed_batch(monkeypatch):
    pending = {"__interrupt__": [SimpleNamespace(id="first", value={"action_requests": [
        {"name": "scale", "args": {}}, {"name": "restart", "args": {}},
    ]})]}
    monkeypatch.setattr(main, "handle_interrupt", Mock(side_effect=[{"type": "approve"}, None]))
    agent = Mock()
    agent.invoke.return_value = pending
    assert main.run_with_hitl(agent, [], {}) is pending
    agent.invoke.assert_called_once()


def test_real_graph_resumes_parallel_interrupts(monkeypatch):
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import interrupt

    from langgraph.checkpoint.memory import InMemorySaver

    def first(state):
        response = interrupt({"action_requests": [{"name": "scale", "args": {}}, {"name": "restart", "args": {}}]})
        assert len(response["decisions"]) == 2
        return {"first": True}

    def second(state):
        response = interrupt({"action_requests": [{"name": "delete", "args": {}}]})
        assert len(response["decisions"]) == 1
        return {"second": True}

    # Separate output keys avoid concurrent updates to a single state channel.
    from typing import TypedDict
    class State(TypedDict, total=False):
        messages: list
        first: bool
        second: bool
    graph = StateGraph(State)
    graph.add_node("first", first)
    graph.add_node("second", second)
    graph.add_edge(START, "first")
    graph.add_edge(START, "second")
    graph.add_edge("first", END)
    graph.add_edge("second", END)
    agent = graph.compile(checkpointer=InMemorySaver())
    monkeypatch.setattr(main, "handle_interrupt", lambda _: {"type": "approve"})
    result = main.run_with_hitl(agent, [], {"configurable": {"thread_id": "cli-test"}})
    assert result["first"] and result["second"]
