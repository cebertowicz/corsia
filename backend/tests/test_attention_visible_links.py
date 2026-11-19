"""Count the link label visible to the reader, not its destination."""

from core.attention_measure import measure_text


def test_link_destination_is_not_counted():
    measured = measure_text('Read [the full report](https://example.com/a-long-path(test)) now.')
    assert measured.words == 5
    assert measured.unspaced == 0


def test_reference_link_and_code_are_counted_as_shown():
    measured = measure_text('Read [the report][ref] and `two words`.\n\n[ref]: https://example.com/hidden-path')
    assert measured.words == 6
