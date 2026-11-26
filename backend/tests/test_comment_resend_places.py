"""A person's comment sent again from a kept draft, with no capture held, lands only on its own passage."""

import httpx
import pytest
from fastapi import FastAPI

from api import doc_comments
from auth_fixtures import OWNER_TOKEN
from doceditor.quote_anchor import _u16, anchor_context
from tests.test_document_live_file_tools import opened
from tests.test_shared_document_ownership import ownership_scope as ownership_scope
from test_doc_image_region_comments import _register, _threads, _url

LEAD = 'An opening paragraph long enough to fill a side.\n\n'
TABLE = LEAD + '| Plan | Ready |\n| --- | --- |\n| Alpha | Yes |\n| Beta | Yes |\n| Gamma | No |\n'


def kept(text: str, quote: str, nth: int = 0) -> dict:
    """The passage fields a kept draft posts: its quote, its offsets when it was written, and both sides."""
    start = -1
    for _ in range(nth + 1):
        start = text.index(quote, start + 1)
    from16 = _u16(text[:start])
    prefix, suffix = anchor_context(text, from16, from16 + _u16(quote), quote)
    return {'anchor_text': quote, 'from_off': from16, 'to_off': from16 + _u16(quote),
            'anchor_prefix': prefix, 'anchor_suffix': suffix}


def span(text: str, quote: str, nth: int = 0) -> tuple[int, int]:
    start = -1
    for _ in range(nth + 1):
        start = text.index(quote, start + 1)
    return _u16(text[:start]), _u16(text[:start]) + _u16(quote)


def resend(client, artifact: str, passage: dict, client_id: str, **extra):
    return client.post(_url(f'/api/artifacts/{artifact}/comments'), json={
        'delivery_policy': 'immediate', 'body': 'Kept words', 'client_id': client_id, **passage, **extra})


def document(workspace, name: str, text: str):
    path = workspace / name
    path.write_text(text)
    return path


def test_a_closed_document_places_a_kept_draft_where_its_passage_moved(integration_client, integration_workspace):
    text = LEAD + 'Budget is final for Q3.\n'
    path = document(integration_workspace, 'resend-shift.md', text)
    artifact = _register(integration_client, path)
    passage = kept(text, 'final')
    path.write_text('A new first line added well before.\n' + text)
    # The draft was captured in a live document that has closed since; its sides still find it.
    response = resend(integration_client, artifact, passage, 'resend-shift', live_capture=True)
    assert response.status_code == 200, response.text
    row = _threads(integration_client, artifact)['resend-shift']
    assert (row['from_off'], row['to_off']) == span(path.read_text(), 'final')
    assert (row['anchor_prefix'], row['anchor_suffix']) == (passage['anchor_prefix'], passage['anchor_suffix'])


def test_a_repeated_word_resent_goes_to_the_copy_it_was_written_on(integration_client, integration_workspace):
    text = LEAD + 'First, ship it.\n\nSecond, ship it again after review.\n'
    path = document(integration_workspace, 'resend-repeat.md', text)
    artifact = _register(integration_client, path)
    passage = kept(text, 'ship', 1)
    path.write_text('Inserted at the top so every offset moves along.\n' + text)
    response = resend(integration_client, artifact, passage, 'resend-repeat')
    assert response.status_code == 200, response.text
    row = _threads(integration_client, artifact)['resend-repeat']
    assert (row['from_off'], row['to_off']) == span(path.read_text(), 'ship', 1)


def test_a_table_cell_resent_is_placed_on_its_own_row(integration_client, integration_workspace):
    path = document(integration_workspace, 'resend-cell.md', TABLE)
    artifact = _register(integration_client, path)
    passage = kept(TABLE, 'Yes', 1)
    path.write_text('Added above the table, far enough away.\n' + TABLE)
    response = resend(integration_client, artifact, passage, 'resend-cell')
    assert response.status_code == 200, response.text
    row = _threads(integration_client, artifact)['resend-cell']
    assert (row['from_off'], row['to_off']) == span(path.read_text(), 'Yes', 1)


EMPTY_CELL = LEAD + '| Plan | Owner |\n| --- | --- |\n| Alpha |   |\n| Beta | Sam |\n'


@pytest.mark.parametrize('shifted', [False, True], ids=['unchanged', 'table shifted'])
def test_an_empty_cell_resent_is_placed_on_its_own_cell(integration_client, integration_workspace, shifted):
    # An empty cell's quote is its own whitespace; its sides reach into the cells around it.
    path = document(integration_workspace, f'resend-empty-cell-{shifted}.md', EMPTY_CELL)
    artifact = _register(integration_client, path)
    passage = kept(EMPTY_CELL, '   ')
    if shifted:
        path.write_text('Added above the table, far enough away.\n' + EMPTY_CELL)
    response = resend(integration_client, artifact, passage, f'resend-empty-cell-{shifted}')
    assert response.status_code == 200, response.text
    row = _threads(integration_client, artifact)[f'resend-empty-cell-{shifted}']
    assert (row['from_off'], row['to_off']) == span(path.read_text(), '   ')


@pytest.mark.parametrize('own', [10, 12], ids=['on the first copy', 'on the second copy'])
def test_two_blank_cells_with_the_same_sides_are_refused_wherever_the_kept_offset_sits(
        integration_client, integration_workspace, own):
    blank = '|   |   |\n'
    text = LEAD + '| Plan | Note |\n| --- | --- |\n' + blank * 12 + '| End | z |\n'
    first, second = span(text, '   ', 10), span(text, '   ', 12)
    assert anchor_context(text, *first, '   ') == anchor_context(text, *second, '   ')  # nothing tells them apart
    path = document(integration_workspace, f'resend-blank-rows-{own}.md', text)
    artifact = _register(integration_client, path)
    response = resend(integration_client, artifact, kept(text, '   ', own), f'resend-blank-rows-{own}')
    assert response.status_code == 409, response.text
    assert response.json()['kind'] == 'passage_gone'
    assert _threads(integration_client, artifact) == {}


@pytest.mark.parametrize('case', ['deleted', 'prefix edited', 'suffix edited', 'row deleted', 'copy added'])
def test_a_kept_draft_whose_passage_cannot_be_told_apart_is_refused_and_not_stored(
        integration_client, integration_workspace, case):
    filler = 'ab' * 20
    text, quote, nth, edit = {
        'deleted': (LEAD + 'Keep this line.\nDrop this line.\nEnd.\n', 'Drop this line.', 0,
                    lambda t: t.replace('Drop this line.\n', '')),
        'prefix edited': (LEAD + 'Budget is final for Q3.\n', 'final', 0, lambda t: t.replace('is final', 'was final')),
        'suffix edited': (LEAD + 'Budget is final for Q3.\n', 'final', 0, lambda t: t.replace('for Q3', 'for Q4')),
        # The other row's 'Yes' has the same words but other sides: it is not this comment's passage.
        'row deleted': (TABLE, 'Yes', 1, lambda t: t.replace('| Beta | Yes |\n', '')),
        # Unique when written; a second copy with the same sides since: both copies survive, and neither is chosen.
        'copy added': (filler + 'word' + filler, 'word', 0, lambda t: t + 'word' + filler),
    }[case]
    path = document(integration_workspace, f'resend-refused-{case.replace(" ", "-")}.md', text)
    artifact = _register(integration_client, path)
    passage = kept(text, quote, nth)
    path.write_text(edit(text))
    response = resend(integration_client, artifact, passage, f'refused-{case.replace(" ", "-")}')
    assert response.status_code == 409, response.text
    assert response.json()['kind'] == 'passage_gone'
    assert _threads(integration_client, artifact) == {}


def test_a_diagram_pin_resent_keeps_its_diagram(integration_client, integration_workspace):
    source = 'graph TD\n A-->B'
    text = LEAD + f'```mermaid\n{source}\n```\n'
    path = document(integration_workspace, 'resend-diagram.md', text)
    artifact = _register(integration_client, path)
    passage = kept(text, '```mermaid')
    path.write_text('Added above the diagram, far enough away.\n' + text)
    response = resend(integration_client, artifact, passage, 'resend-diagram',
                      region={'x': 0.2, 'y': 0.3}, diagram_source=source)
    assert response.status_code == 200, response.text
    row = _threads(integration_client, artifact)['resend-diagram']
    assert (row['from_off'], row['to_off']) == span(path.read_text(), '```mermaid')
    assert row['image_rev']


async def post_live(entry, passage: dict, client_id: str):
    app = FastAPI()
    app.include_router(doc_comments.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        return await client.post(f'/api/artifacts/{entry.artifact_id}/comments', params={'token': OWNER_TOKEN}, json={
            'delivery_policy': 'immediate', 'body': 'Kept words', 'client_id': client_id, 'live_capture': True, **passage})


@pytest.mark.asyncio
async def test_an_open_document_with_no_capture_held_places_a_kept_draft_by_its_sides(ownership_scope, tmp_path, monkeypatch):
    from infra import app_loop
    import asyncio
    manager, entry, _ = await opened(ownership_scope, tmp_path, monkeypatch)
    app_loop.capture(asyncio.get_running_loop())
    try:
        text = entry.live.text
        placed = await post_live(entry, kept(text, 'second'), 'live-resend')
        assert placed.status_code == 200, placed.text
        assert (placed.json()['comment']['from_off'], placed.json()['comment']['to_off']) == span(text, 'second')
        assert entry.live.ranges['live-resend'] == span(text, 'second')
        refused = await post_live(entry, {**kept(text, 'second'), 'anchor_prefix': 'not its side'}, 'live-refused')
        assert refused.status_code == 409, refused.text
        assert refused.json()['kind'] == 'passage_gone'
        assert 'live-refused' not in entry.live.ranges
    finally:
        await manager.close()
        app_loop.clear()
