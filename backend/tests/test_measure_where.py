"""KPI predicates refuse presence tests on enums at sync."""
import pytest

from infra.measure_validation import Lines, load, validate


def declaration(predicate):
    return f"""schema: corsia.measures/1
namespace: demo
events:
  demo.done:
    attributes: {{key: id, state: {{enum: [open, closed]}}}}
    dedupe: [key]
kpis:
  count:
    title: Count
    unit: count
    better: higher
    formula: count
    num: {{event: demo.done, where: {predicate}}}
    min_n: 1
"""


@pytest.mark.parametrize("predicate", ["{state: present}", "{state: absent}"])
def test_enum_presence_is_refused(predicate):
    text = declaration(predicate)
    with pytest.raises(ValueError, match="only on id"):
        validate(load(text), Lines(text))


@pytest.mark.parametrize("predicate", ["{key: present}", "{key: absent}", "{state: [open, closed]}"])
def test_id_presence_and_enum_values_are_accepted(predicate):
    text = declaration(predicate)
    validate(load(text), Lines(text))
