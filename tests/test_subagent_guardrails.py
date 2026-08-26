"""Construction tests for worker context and cost guardrails."""
from __future__ import annotations

import agent

from agent import _build_subagents
from subagents import ALL_SUBAGENTS
from tools import READ_TOOLS


def _middleware_types(spec: dict) -> set[str]:
    return {type(middleware).__name__ for middleware in spec["middleware"]}


def test_every_worker_receives_the_shared_guardrails():
    subagents = _build_subagents(READ_TOOLS)

    assert len(subagents) == len(ALL_SUBAGENTS) + 1
    assert {subagent["name"] for subagent in subagents} == {
        *(subagent["name"] for subagent in ALL_SUBAGENTS),
        "general-purpose",
    }
    for subagent in subagents:
        middleware_types = _middleware_types(subagent)
        assert "truncate_tool_output" in middleware_types
        assert "ModelCallLimitMiddleware" in middleware_types
        assert "ToolCallLimitMiddleware" in middleware_types


def test_workers_receive_fresh_stateful_guardrail_instances():
    first = {subagent["name"]: subagent for subagent in _build_subagents(READ_TOOLS)}
    second = {subagent["name"]: subagent for subagent in _build_subagents(READ_TOOLS)}

    for name in first:
        first_stateful = [
            middleware
            for middleware in first[name]["middleware"]
            if type(middleware).__name__ in {"ModelCallLimitMiddleware", "ToolCallLimitMiddleware"}
        ]
        second_stateful = [
            middleware
            for middleware in second[name]["middleware"]
            if type(middleware).__name__ in {"ModelCallLimitMiddleware", "ToolCallLimitMiddleware"}
        ]
        assert len(first_stateful) == len(second_stateful)
        assert all(left is not right for left, right in zip(first_stateful, second_stateful))


def test_orchestrator_passes_only_guarded_workers_to_deep_agents(monkeypatch):
    captured = {}

    def capture_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agent, "create_deep_agent", capture_create_deep_agent)
    agent.create_sre_agent(checkpointer=object(), store=object())

    assert {subagent["name"] for subagent in captured["subagents"]} == {
        *(subagent["name"] for subagent in ALL_SUBAGENTS),
        "general-purpose",
    }
    assert all("middleware" in subagent for subagent in captured["subagents"])
