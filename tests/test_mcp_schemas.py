"""The MCP tool surface is what agents actually call.

A decorator that loses the wrapped function's signature still passes every
service-level test, because those call the services directly. These tests
exercise the generated JSON schemas instead, which is the only place such a
loss becomes visible.
"""

import asyncio

import pytest

from src.mcp_server import mcp

EXPECTED_TOOL_COUNT = 47


@pytest.fixture(scope="module")
def tools():
    return asyncio.run(mcp.list_tools())


def test_every_tool_is_registered(tools):
    assert len(tools) == EXPECTED_TOOL_COUNT


def test_no_tool_leaks_varargs_into_its_schema(tools):
    leaked = [
        tool.name
        for tool in tools
        if {"args", "kwargs"} & set((tool.input_schema or {}).get("properties", {}))
    ]
    assert leaked == [], f"tools exposing *args/**kwargs instead of real parameters: {leaked}"


def test_every_tool_describes_itself(tools):
    undocumented = [tool.name for tool in tools if not (tool.description or "").strip()]
    assert undocumented == []


@pytest.mark.parametrize(
    ("tool_name", "required"),
    [
        ("get_trial_balance", set()),
        ("get_partner", {"partner"}),
        ("create_purchase_order", {"supplier", "warehouse", "lines"}),
        ("receive_goods", {"order"}),
        ("post_vendor_bill", {"order"}),
    ],
)
def test_required_parameters_match_the_signature(tools, tool_name, required):
    tool = next(t for t in tools if t.name == tool_name)
    assert set((tool.input_schema or {}).get("required") or []) == required
