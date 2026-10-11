"""Independent Slack transport diagnostics without message contents."""
from datetime import datetime, timezone
import json
import logging
import re
import sys
import traceback
from pathlib import Path
from threading import RLock


def safe_error_type(error):
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else "TransportError"


class SlackDeliveryError(Exception):
    def __init__(self, error_type, retry_after_seconds=None):
        self.error_type = error_type
        self.retry_after_seconds = retry_after_seconds
        super().__init__(error_type)


def delivery_error(error):
    retry_after = None
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) == 429:
        try:
            headers = response.headers
            retry_after = max(0, float(headers.get("Retry-After", headers.get("retry-after", 0))))
        except (TypeError, ValueError, AttributeError):
            pass
    return SlackDeliveryError(safe_error_type(error), retry_after)


class SlackRuntimeHealth:
    def __init__(self):
        self._lock = RLock()
        self._client = None
        self._state = {
            "socket": {"state": "disabled", "connected": False, "transitions": 0,
                       "errors": 0, "last_transition_at": None, "last_connected_at": None,
                       "last_event_at": None, "last_error_at": None, "last_error_type": None,
                       "last_error_frames": []},
            "outbound": {"state": "disabled", "successes": 0, "failures": 0,
                         "last_success_at": None, "last_error_at": None, "last_error_type": None},
        }

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def outbound_enabled(self, enabled):
        with self._lock:
            self._state["outbound"]["state"] = "unknown" if enabled else "disabled"

    def socket_state(self, state):
        with self._lock:
            row = self._state["socket"]
            if row["state"] != state:
                row["transitions"] = min(row["transitions"] + 1, 2**63 - 1)
                row["last_transition_at"] = self._now()
                if state == "connected":
                    row["last_connected_at"] = row["last_transition_at"]
            row["state"] = state
            row["connected"] = state == "connected"

    def socket_error(self, error):
        with self._lock:
            row = self._state["socket"]
            row["errors"] = min(row["errors"] + 1, 2**63 - 1)
            row["last_error_at"] = self._now()
            row["last_error_type"] = safe_error_type(error)
            row["last_error_frames"] = [
                {"module": Path(frame.filename).stem[:80], "function": frame.name[:80], "line": frame.lineno}
                for frame in traceback.extract_tb(error.__traceback__)[-8:]
            ]

    def socket_message(self, message):
        try:
            envelope = json.loads(message)
        except (TypeError, ValueError):
            return
        with self._lock:
            self._state["socket"]["last_event_at"] = self._now()
        if isinstance(envelope, dict) and envelope.get("type") == "hello":
            self.socket_state("connected")

    def outbound_result(self, error=None):
        with self._lock:
            row = self._state["outbound"]
            key = "failures" if error else "successes"
            row[key] = min(row[key] + 1, 2**63 - 1)
            row["state"] = "degraded" if error else "healthy"
            row["last_error_at" if error else "last_success_at"] = self._now()
            if error:
                row["last_error_type"] = safe_error_type(error)

    def status(self):
        with self._lock:
            if self._client is not None:
                try:
                    self.socket_state("connected" if self._client.is_connected() else "disconnected")
                except Exception as error:
                    self.socket_error(error)
                    self.socket_state("disconnected")
            return {key: {**value, **({"last_error_frames": [dict(f) for f in value["last_error_frames"]]} if "last_error_frames" in value else {})} for key, value in self._state.items()}


class SafeSocketLogger(logging.Logger):
    """Keep lifecycle signals without SDK exception text, frames, or payloads."""
    def __init__(self, health):
        super().__init__("sre-agent.slack.socket", logging.INFO)
        self.parent = logging.getLogger("sre-agent.slack")
        self.health = health

    def _log(self, level, msg, args, exc_info=None, extra=None, stack_info=False, stacklevel=1):
        event = "socket_lifecycle"
        text = str(msg)
        if "Reconnecting" in text or "reconnect" in text:
            event = "socket_reconnect"
        elif text.startswith("A new session has been established"):
            event = "socket_connected"
        elif text.startswith("Stopped receiving messages"):
            event = "socket_receive_stopped"
        elif text.startswith("Starting to receive messages"):
            event = "socket_receive_started"
        if level >= logging.ERROR:
            event = "socket_error"
            info = sys.exc_info() if exc_info is True else exc_info
            error = info[1] if isinstance(info, tuple) else None
            self.health.socket_error(error or RuntimeError())
        super()._log(level, "Slack SDK %s", (event,), exc_info=None, stack_info=False,
                     stacklevel=stacklevel)



class SafeWebLogger(logging.Logger):
    """Keep HTTP SDK diagnostics from logging headers or report bodies."""
    def __init__(self):
        super().__init__("sre-agent.slack.web", logging.INFO)
        self.parent = logging.getLogger("sre-agent.slack")

    def _log(self, level, msg, args, exc_info=None, extra=None, stack_info=False, stacklevel=1):
        super()._log(level, "Slack Web API transport event", (), exc_info=None,
                     stack_info=False, stacklevel=stacklevel)

def install_socket_health(handler, health):
    """Use built-in Socket Mode listeners and connection readback."""
    client = handler.client
    health._client = client
    health.socket_state("connecting")
    client.logger = SafeSocketLogger(health)
    client.trace_enabled = False
    client.all_message_trace_enabled = False
    client.ping_pong_trace_enabled = False
    client.on_message_listeners.append(health.socket_message)
    client.on_error_listeners.append(health.socket_error)
    client.on_close_listeners.append(lambda code, reason: health.socket_state("disconnected"))
