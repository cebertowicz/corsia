"""Diagram revisions remain tied to their own blocks when listing comments."""

from doceditor import diagram_pins


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
    assert [row['region_stale'] for row in rows] == [False, True, False, True, False]
