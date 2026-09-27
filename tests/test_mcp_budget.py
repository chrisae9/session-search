"""MCP callers discover byte budgets and invalid requests never reach retrieval."""

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from session_search.interfaces.mcp import create_mcp


class RecordingClient:
    def __init__(self):
        self.calls = []

    def read(self, operation, payload):
        self.calls.append((operation, payload))
        item = ({'excerpt': 'Ω' * 40000} if operation == 'search' else
                {'citation': {'event_id': 'synthetic'},
                 'events': [{'event_id': 'synthetic', 'text': 'Ω' * 40000}]})
        return {'version': 1, 'status': 'ok', 'results': [item]}


@pytest.mark.parametrize('name,default', [('search', 16384), ('context', 32768)])
def test_budget_contract_is_discoverable(tmp_path, name, default):
    tools = asyncio.run(create_mcp(tmp_path).list_tools())
    tool = next(tool for tool in tools if tool.name == name)
    budget = tool.inputSchema['properties']['budget']
    assert budget['type'] == 'integer'
    assert budget['minimum'] == 1024
    assert budget['maximum'] == 65536
    assert budget['default'] == default
    assert 'budget' not in tool.inputSchema.get('required', [])
    for description in (budget['description'], tool.description):
        assert 'UTF-8' in description
        assert 'metadata' in description
        assert '1024' in description and '65536' in description
    assert str(default) in tool.description


@pytest.mark.parametrize('name,args', [('search', {'text': 'synthetic'}),
                                      ('context', {'citations': []})])
@pytest.mark.parametrize('budget', [1023, 65537])
def test_invalid_budget_rejected_before_retrieval(tmp_path, name, args, budget):
    client = RecordingClient()
    server = create_mcp(tmp_path, client=client)
    with pytest.raises(ToolError, match='budget'):
        asyncio.run(server.call_tool(name, {**args, 'budget': budget}))
    assert client.calls == []


@pytest.mark.parametrize('name,args,default', [('search', {'text': 'synthetic'}, 16384),
                                              ('context', {'citations': []}, 32768)])
@pytest.mark.parametrize('budget', [None, 1024, 65536])
def test_valid_budget_and_defaults_bound_utf8_output(tmp_path, name, args, default, budget):
    client = RecordingClient()
    server = create_mcp(tmp_path, client=client)
    supplied = args if budget is None else {**args, 'budget': budget}
    result = asyncio.run(server.call_tool(name, supplied))
    assert len(client.calls) == 1
    assert client.calls[0][0] == name
    assert len(result[0].text.encode('utf-8')) <= (default if budget is None else budget)
    assert json.loads(result[0].text)['truncated'] is True
