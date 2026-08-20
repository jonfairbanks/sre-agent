"""Convert LangChain message content into displayable plain text."""
from __future__ import annotations

from typing import Any


def response_text(message_or_content: Any) -> str:
    """Return text from a LangChain message or a structured content payload.

    OpenAI's Responses API may represent an assistant response as a list of
    content blocks (for example ``[{"type": "text", "text": "..."}]``).
    Passing that list to a browser or Slack coerces each dict to an unhelpful
    object representation, so callers must normalize it before display.
    """
    content = getattr(message_or_content, "content", message_or_content)
    return _content_text(content)


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list) or isinstance(content, tuple):
        return "\n".join(filter(None, (_content_text(block) for block in content)))
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        if text is not None:
            nested = _content_text(text)
            if nested:
                return nested
        content = content.get("content")
        if content is not None:
            return _content_text(content)
        return ""
    return str(content)
