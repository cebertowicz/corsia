"""SQL fragments that read a plan decision's answer, for either pick kind."""

from __future__ import annotations

# Pick-any answers (a set of option ids, possibly empty) count through the same
# three buckets as pick one, and the partition is total: every answered pick-any
# is exactly one of took-the-recommendation, other, or neither. A body written
# before pick-any has no ``pick`` and is pick one, so its expressions are
# unchanged. Sets compare as sorted, unique ids, so click order never matters.
IS_ANY = "COALESCE(json_extract(metadata,'$.plan_decision.pick'),'one')='any'"
ANY_CHOSEN = (
    "(SELECT json_group_array(value) FROM (SELECT DISTINCT value FROM json_each("
    "COALESCE(json_extract(metadata,'$.plan_decision.answer.option_ids'),'[]')) ORDER BY value))"
)
ANY_RECOMMENDED = (
    "(SELECT json_group_array(value) FROM (SELECT DISTINCT value FROM json_each("
    "CASE WHEN json_type(metadata,'$.plan_decision.recommendation')='array' "
    "THEN json_extract(metadata,'$.plan_decision.recommendation') ELSE '[]' END) ORDER BY value))"
)
ANY_TOOK = f"({ANY_CHOSEN}={ANY_RECOMMENDED})"
ANY_OTHER = (
    f"(CASE WHEN {ANY_CHOSEN}='[]' AND {ANY_RECOMMENDED}!='[]' "
    "AND TRIM(COALESCE(json_extract(metadata,'$.plan_decision.answer.text'),''))!='' "
    "THEN 1 ELSE 0 END)"
)
