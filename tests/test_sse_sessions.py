"""Session event broadcasts, reconnect outcomes, and connection cleanup."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import api


@pytest.fixture(autouse=True)
def isolated_sessions(monkeypatch):
    monkeypatch.setattr(api, "_sessions", {})
    monkeypatch.setattr(api, "_db", None)
    monkeypatch.setattr(api, "_notifier", None)


async def open_stream(session):
    api._sessions[session.id] = session
    response = await api.stream_events(session.id)
    stream = response.body_iterator
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    return stream, pending


@pytest.mark.parametrize("kind,data", [
    ("done", "Complete"), ("interrupt", ["Approval Required"]), ("error", "Failed"),
])
def test_two_subscribers_receive_the_same_terminal_event(kind, data):
    async def scenario():
        session = api.Session("shared", "shared", status=api.SessionStatus.RUNNING)
        first, first_event = await open_stream(session)
        second, second_event = await open_stream(session)
        assert len(session.subscribers) == 2
        event = {"type": kind, "data": data}
        await session.publish(event)
        assert json.loads(await asyncio.wait_for(first_event, 1)) == event
        assert json.loads(await asyncio.wait_for(second_event, 1)) == event
        for stream in (first, second):
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
        assert not session.subscribers
    asyncio.run(scenario())


def test_disconnect_removes_only_its_subscription():
    async def scenario():
        session = api.Session("shared", "shared", status=api.SessionStatus.RUNNING)
        first, first_event = await open_stream(session)
        second, second_event = await open_stream(session)
        first_event.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_event
        assert len(session.subscribers) == 1
        await session.publish({"type": "done", "data": "Complete"})
        assert json.loads(await asyncio.wait_for(second_event, 1))["type"] == "done"
        await second.aclose()
        await first.aclose()
        assert not session.subscribers
    asyncio.run(scenario())


@pytest.mark.parametrize("status,kind,data", [
    (api.SessionStatus.DONE, "done", "Complete"),
    (api.SessionStatus.INTERRUPTED, "interrupt", ["Approval Required"]),
    (api.SessionStatus.ERROR, "error", "The agent run failed. Start a new request."),
])
def test_recovered_terminal_state_is_sent_and_stream_closes(status, kind, data):
    async def scenario():
        original = api.Session("recovered", "recovered", status=status,
                               last_response="Complete", interrupt_data=["Approval Required"])
        session = api.Session.from_row(original.to_row())
        for _ in range(2):
            stream, pending = await open_stream(session)
            assert json.loads(await asyncio.wait_for(pending, 1)) == {"type": kind, "data": data}
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert not session.subscribers
    asyncio.run(scenario())


def test_error_reconnect_retains_current_process_error():
    async def scenario():
        session = api.Session("error", "error", status=api.SessionStatus.ERROR)
        await session.publish({"type": "error", "data": "Agent unavailable"})
        stream, pending = await open_stream(session)
        assert json.loads(await asyncio.wait_for(pending, 1))["data"] == "Agent unavailable"
        await stream.aclose()
    asyncio.run(scenario())


def test_slow_subscriber_drops_old_progress_and_keeps_terminal():
    async def scenario():
        session = api.Session("bounded", "bounded", status=api.SessionStatus.RUNNING)
        slow = session.subscribe()
        fast = session.subscribe()
        for index in range(100):
            event = {"type": "todos", "data": index}
            await session.publish(event)
            assert await fast.get() == event
        terminal = {"type": "done", "data": "Complete"}
        await session.publish(terminal)
        assert await fast.get() == terminal
        assert slow.qsize() == slow.maxsize == 32
        buffered = [slow.get_nowait() for _ in range(slow.qsize())]
        assert buffered[0]["data"] == 69
        assert buffered[-1] == terminal
    asyncio.run(scenario())


def test_agent_result_publishes_todos_before_completion():
    async def scenario():
        session = api.Session("result", "result", status=api.SessionStatus.RUNNING)
        queue = session.subscribe()
        result = {"messages": [SimpleNamespace(content="Complete")],
                  "todos": [{"content": "Inspect Pods", "status": "completed"}]}
        await asyncio.to_thread(api._handle_result, result, session, asyncio.get_running_loop())
        assert (await asyncio.wait_for(queue.get(), 1))["type"] == "todos"
        assert await asyncio.wait_for(queue.get(), 1) == {"type": "done", "data": "Complete"}
        assert session.status == api.SessionStatus.DONE
    asyncio.run(scenario())


@pytest.mark.parametrize("resume", ["approve", "reject", "edit", "chat"])
def test_starting_next_run_preserves_active_subscribers(monkeypatch, resume):
    async def scenario():
        session = api.Session("resume", "resume", status=api.SessionStatus.INTERRUPTED)
        api._sessions[session.id] = session
        queue = session.subscribe()
        queue.get_nowait()
        session.terminal_event = {"type": "interrupt", "data": ["Old Approval"]}
        # Exercise the real transition while keeping agent work out of this test.
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "run_in_executor", lambda *args: None)
        if resume == "approve":
            api._do_approve(session, loop)
        elif resume == "reject":
            api._do_reject(session, "Skip", loop)
        elif resume == "edit":
            await api.edit(api.EditRequest(session_id=session.id, args={}))
        else:
            await api.chat(api.ChatRequest(session_id=session.id, message="Continue"))
        assert session.status == api.SessionStatus.RUNNING
        assert session.terminal_event is None
        await session.publish({"type": "done", "data": "Next Result"})
        assert await queue.get() == {"type": "done", "data": "Next Result"}
    asyncio.run(scenario())


def test_http_stream_keeps_native_eventsource_json_contract():
    from fastapi.testclient import TestClient

    session = api.Session("http", "http", status=api.SessionStatus.DONE, last_response="Complete")
    api._sessions[session.id] = session
    response = TestClient(api.app).get(f"/api/sessions/{session.id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = [line.removeprefix("data: ") for line in response.text.splitlines()
                if line.startswith("data: ")]
    assert [json.loads(payload) for payload in payloads] == [{"type": "done", "data": "Complete"}]
    assert not session.subscribers
