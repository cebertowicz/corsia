"""Diagram revisions remain tied to their own blocks when listing comments."""

import json
from pathlib import Path

import pytest

from doceditor import diagram_pins


CASES = json.loads((Path(__file__).parent / 'fixtures' / 'diagram-fences.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_shared_diagram_fixtures(case):
    block = case['block']
    expected = (block['first'], block['last'], block['source']) if block else None
    assert diagram_pins.block_at(case['markdown'], case['anchor']) == expected


def test_stale_diagram_move_is_refused(tmp_path):
    from doceditor.comment_action_errors import CommentSelectionChanged
    from doceditor.doc_images import region_patch

    path = tmp_path / 'diagram.md'
    path.write_text('```mermaid\ngraph TD\n```\n')
    comment = {'region_number': 1, 'from_off': 0}
    with pytest.raises(CommentSelectionChanged, match='Place the pin on the current diagram'):
        region_patch(None, comment, {'region': {'x': .5, 'y': .5}, 'diagram_source': 'old'},
                     doc_path=str(path), owner_thread_id=None, require_diagram_source=True)


def test_listing_marks_each_diagram_against_its_own_revision(tmp_path):
    text = '😀\n```mermaid\ngraph TD\n A-->B\n```\n\n```mermaid\ngraph TD\n B-->C\n```\n'
    path = tmp_path / 'diagrams.md'
    path.write_text(text)
    def offset(source):
        return len(text[:text.index(source)].encode('utf-16-le')) // 2

    first, second = offset('```mermaid'), offset('```mermaid\ngraph TD\n B-->C')
    first_rev, second_rev = diagram_pins.revision(text, first), diagram_pins.revision(text, second)
    rows = [
        {'from_off': first, 'image_rev': first_rev},
        {'from_off': first, 'image_rev': 'older'},
        {'from_off': second, 'image_rev': second_rev},
        {'from_off': second, 'image_rev': first_rev},
        {'from_off': 0, 'image_rev': 'unknown'},
    ]
    diagram_pins.mark_stale(rows, str(path))
    assert [row['region_stale'] for row in rows] == [False, True, False, True, True]
