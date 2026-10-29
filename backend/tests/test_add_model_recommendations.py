"""The Add model list uses the Mac's room, including models already on disk."""

import pytest

from api import provider_local_models as route
from llm.local_model import LocalModel

GIB = 1024**3


@pytest.mark.asyncio
async def test_recommendations_use_total_room_and_include_loaded_build(monkeypatch, tmp_path):
    loaded = LocalModel('org/Example-oQ4e', 10 * GIB, 32768, resident_bytes=40 * GIB)
    other = LocalModel('org/Example-oQ6e', 20 * GIB, 32768, resident_bytes=60 * GIB)
    too_large = LocalModel('org/Large-oQ4e', 80 * GIB, 32768, resident_bytes=120 * GIB)

    async def server():
        return {'id': 'local'}

    async def disk(_cfg):
        return []

    async def running(_cfg):
        return [{'identifier': loaded.model, 'size_bytes': 40 * GIB}]

    async def recommendations(_server_id):
        return {'best': [loaded, other, too_large]}

    async def model_dir(_server_id):
        return tmp_path

    monkeypatch.setattr(route, 'local_memory_ok', lambda: True)
    monkeypatch.setattr(route, '_server', server)
    monkeypatch.setattr(route, 'host_ram_bytes', lambda: 128 * GIB)
    monkeypatch.setattr('infra.host_resources.host_total_ram_bytes', lambda: 128 * GIB)
    monkeypatch.setattr('infra.host_resources.host_available_ram_bytes', lambda: 72 * GIB)
    monkeypatch.setattr(route, 'omlx_version', lambda: '0.7.1')
    monkeypatch.setattr(route, 'omlx_has_recommender', lambda _: True)
    monkeypatch.setattr(route, 'ask_recommendations', recommendations)
    monkeypatch.setattr(route, 'download_dir', model_dir)
    monkeypatch.setattr(route, 'download_disk_space', lambda *_: {'state': 'ready', 'free_bytes': 100 * GIB})
    monkeypatch.setattr('llm.providers.omlx_lifecycle.list_disk_models', disk)
    monkeypatch.setattr('llm.providers.omlx_lifecycle.list_loaded', running)
    monkeypatch.setattr(route.local_downloads, 'all', lambda: [])
    monkeypatch.setattr(route.local_downloads, 'reconcile', lambda _: None)

    result, _ = await route._recommendations()
    views = {model['repository']: model for model in result['models']}
    assert views[loaded.repository]['on_disk'] is True
    assert views[other.repository]['on_disk'] is False
    assert views[other.repository]['fits_now'] is True
    assert views[too_large.repository]['fits_now'] is False
    assert result['memory_in_use_gb'] == 40
    assert result['recommended_repository'] == loaded.repository
