import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth_fixtures import OWNER_SESSION_TOKEN, OWNER_TOKEN


def _client():
    from api.system import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _patch(client, token, body):
    return client.patch('/api/settings', headers={'Authorization': f'Bearer {token}'}, json=body)


@pytest.mark.asyncio
async def test_conductor_saves_fallback_consumed_by_runner(isolated_db, monkeypatch):
    from infra.settings import get_agent_defaults, set_setting
    from reviewer.reviewer_runner import _cc_fallback_for_base
    from tests.test_corsia_api_tool import _actors
    from tools.corsia_api import CorsiaApiTool

    set_setting('agents', {'defaults': {'reviewer_fallback_model': 'old'}})
    monkeypatch.delenv('CORSIA_REVIEWER_FALLBACK_MODEL', raising=False)
    conductor, _, _ = _actors()
    result = json.loads(await CorsiaApiTool().execute(
        'PATCH', '/api/settings', body={'reviewer_fallback_model': 'claude-code/opus'},
        _thread_id=conductor,
    ))
    assert result['status'] == 200, result
    assert result['data']['reviewer_fallback_model'] == 'claude-code/opus'
    assert get_agent_defaults()['reviewer_fallback_model'] == 'claude-code/opus'
    assert _cc_fallback_for_base('dev')[0] == 'claude-code/opus'
    cleared = json.loads(await CorsiaApiTool().execute(
        'PATCH', '/api/settings', body={'reviewer_fallback_model': None},
        _thread_id=conductor,
    ))
    assert cleared['status'] == 403
    assert get_agent_defaults()['reviewer_fallback_model'] == 'claude-code/opus'


def test_person_saves_primary_and_response_echoes_it(isolated_db):
    from infra.settings import get_agent_defaults

    with _client() as client:
        response = _patch(client, OWNER_SESSION_TOKEN, {'reviewer_model': 'codex/gpt-5-codex'})
    assert response.status_code == 200
    assert response.json()['reviewer_model'] == 'codex/gpt-5-codex'
    assert get_agent_defaults()['reviewer_model'] == 'codex/gpt-5-codex'


def test_unknown_model_rejects_all_changes(isolated_db):
    from infra.settings import get_agent_defaults, set_setting

    defaults = {'reviewer_model': 'codex/gpt-5-codex', 'reviewer_fallback_model': 'claude-code/opus'}
    set_setting('agents', {'defaults': defaults})
    with _client() as client:
        response = _patch(client, OWNER_SESSION_TOKEN, {
            'reviewer_model': 'claude-code/opus', 'reviewer_fallback_model': 'unknown/model',
        })
    assert response.status_code == 400
    assert response.json()['detail'] == "That model isn't in the catalog. Choose another model."
    assert get_agent_defaults() == defaults


def test_machine_token_cannot_set_reviewer_models(isolated_db):
    from infra.settings import get_agent_defaults, set_setting

    defaults = {'reviewer_model': 'codex/gpt-5-codex', 'reviewer_fallback_model': 'claude-code/opus'}
    set_setting('agents', {'defaults': defaults})
    with _client() as client:
        for key in defaults:
            response = _patch(client, OWNER_TOKEN, {key: 'codex/gpt-5-codex'})
            assert response.status_code == 403
    assert get_agent_defaults() == defaults


def test_primary_cannot_clear_and_fallback_can_clear(isolated_db):
    from infra.settings import get_agent_defaults, set_setting

    defaults = {'reviewer_model': 'codex/gpt-5-codex', 'reviewer_fallback_model': 'claude-code/opus'}
    set_setting('agents', {'defaults': defaults})
    with _client() as client:
        refused = _patch(client, OWNER_SESSION_TOKEN, {
            'reviewer_model': None, 'reviewer_fallback_model': None,
        })
        assert refused.status_code == 400
        assert get_agent_defaults() == defaults
        cleared = _patch(client, OWNER_SESSION_TOKEN, {'reviewer_fallback_model': None})
    assert cleared.status_code == 200
    assert cleared.json()['reviewer_fallback_model'] is None
    assert get_agent_defaults() == {'reviewer_model': 'codex/gpt-5-codex'}
