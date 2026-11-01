"""Scoped Claude discovery uses only its own prior context identity evidence."""
import json
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('prior,probe', [
    ({'discovery': {'models': {'claude-code/sonnet': {'resolved_identity': 'exact', 'context_window': 200000}}}}, False),
    ({'discovery': {'models': {'claude-code/sonnet': {'resolved_identity': 'exact'}}}}, True),
    ({'discovery': {'models': {'claude-code/sonnet': {'resolved_identity': 'old', 'context_window': 200000}}}}, True),
    (None, True),
])
async def test_actual_scoped_discovery_respects_own_prior_snapshot(monkeypatch, prior, probe):
    from connections import claude_discovery
    from connections.account_discovery_types import StagedDiscovery

    context = SimpleNamespace(prior_discovery=prior)
    calls = []
    async def control(binary, frames, **kwargs):
        assert binary == '/synthetic/claude'
        assert kwargs == {'operation': 'list_models', 'context': context}
        assert [frame['request_id'] for frame in frames] == ['corsia-model-list', 'corsia-usage', 'corsia-model-list-end']
        return json.dumps({'type': 'control_response', 'response': {
            'request_id': 'corsia-model-list', 'subtype': 'success',
            'response': {'models': [{'value': 'sonnet', 'resolvedModel': 'exact'}]}}}).encode()
    async def context_probe(binary, selector, **kwargs):
        assert binary == '/synthetic/claude' and selector == 'sonnet'
        assert kwargs == {'context': context}
        calls.append(selector)
        return {'rawMaxTokens': 200000}
    monkeypatch.setattr(claude_discovery, 'run_claude_control', control)
    monkeypatch.setattr(claude_discovery, '_discover_claude_context', context_probe)
    result = await claude_discovery.discover_claude_models('/synthetic/claude', context=context)
    assert isinstance(result, StagedDiscovery)
    assert calls == (['sonnet'] if probe else [])
    assert result.payload['models'] == [{'value': 'sonnet', 'resolvedModel': 'exact',
                                         **({'contextUsage': {'rawMaxTokens': 200000}} if probe else {})}]
