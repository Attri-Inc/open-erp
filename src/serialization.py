"""Presentation/serialization helpers for the MCP transport.

Keeps `json` out of the tool layer: tools call `tool_response(...)` /
`error_response(...)` and never format envelopes by hand, so the wire format
lives in exactly one place.
"""

import json
from typing import Any


def tool_response(payload: Any) -> str:
    """Serialize a successful tool result to the JSON string MCP expects."""
    return json.dumps(payload, default=str)


def error_response(message: str) -> str:
    """The standard error envelope, serialized."""
    return json.dumps({"error": message}, default=str)
