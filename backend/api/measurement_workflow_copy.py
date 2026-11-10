"""Wording for the workflow verdict engine's reason and outcome words."""
from typing import Any, Mapping

# Reasons in the approved workflow view's row-5 wording. Counts are included only when the engine provides them.
REASON_COPY = {
    "local_edit": "Some runs used a workflow edit that wasn't saved as a version, so they aren't compared.",
    "unversioned": "Some runs have no version recorded, so they aren't compared.",
    "no_shared_groups": "The versions don't have comparable data for the same model, difficulty and size of work yet.",
    "coverage_under_half": "A measure is recorded on fewer than half of one version's runs.",
    "counter_changed": "The way corrections are counted changed, so the versions can't be compared on that measure.",
    "kpi_unconfirmed": "Some measure values aren't confirmed yet.",
    "cost_unknown": "Cost per outcome is unavailable for this comparison.",
    "no_baseline": "First version: nothing earlier to compare with.",
    "no_independent_quality_measure": "CORSIA can't compare the workflow owner's measures across versions yet.",
    "kpi_engine_missing": "CORSIA can't compare the workflow owner's measures across versions yet.",
    "replaced_before_look_N": "The earlier version was replaced before a call.",
    "changed_early": "The earlier version was replaced before a call.",
}

OUTCOME_COPY = {
    "quality_disagree": "Some quality measures improved, but another got worse; the comparison supports reverting.",
    "quality_worse": "A quality measure got worse; the comparison supports reverting.",
    "quality_better": "A quality measure improved; the comparison supports keeping this version.",
    "cost_worse_no_gain": "Cost got worse without a quality gain; the comparison supports reverting.",
    "no_gain_not_cheaper": "The comparison found no quality gain or lower cost; it supports reverting.",
    "cheaper_small_harm": "Cost was lower, but quality showed a small harm; the comparison supports reverting.",
    "no_worse": "Quality was no worse and cost was lower; the comparison supports keeping this version.",
    "unclear_after_look_3": "The result remained unclear at the last check; the comparison supports reverting.",
    "queued_after_look_2": "The result remained unclear at the second check; the comparison supports reverting.",
}


def summary(verdict: Mapping[str, Any] | None, excluded: Mapping[str, Any], owner: bool,
            calls: list[Mapping[str, Any]] | None = None) -> list[str]:
    """Describe only the engine's actual verdict, reasons and next-look inputs."""
    lines = []
    if verdict:
        word, reason = verdict.get("word"), str(verdict.get("reason") or "")
        if word == "collecting":
            lines.append("Too early to decide.")
            inputs = verdict.get("inputs") or {}
            n, minimum = inputs.get("n"), inputs.get("minimum")
            if type(n) is int and type(minimum) is int and n < minimum:
                lines.append(f"{n} of {minimum} comparable results; next check at {minimum}.")
            elif type(n) is int and type(minimum) is int:
                lines.append("There isn't enough comparable data for the next check yet.")
            if owner:
                lines.append("CORSIA will try to tell the workflow owner when a result is reached.")
        elif word == "not_enough_data":
            inputs = verdict.get("inputs") or {}
            if reason == "no_baseline" and inputs.get("skipped"):
                lines.append("No earlier version has enough settled runs to compare yet.")
            elif reason == "kpi_unconfirmed" and type(inputs.get("k")) is int and inputs.get("kpi"):
                count = inputs["k"]
                lines.append(f"{count} {'value' if count == 1 else 'values'} for {inputs['kpi']} {'isn’t' if count == 1 else 'aren’t'} confirmed yet.")
            else:
                lines.append(REASON_COPY.get(reason, "There isn't enough data to compare these versions."))
        elif word == "changed_early":
            inputs = verdict.get("inputs") or {}
            count = inputs.get("k")
            lines.append(f"The earlier version was replaced after {count} runs, before a call."
                         if type(count) is int else REASON_COPY["changed_early"])
        elif word in {"keep", "revert"}:
            lines.append(OUTCOME_COPY.get(reason, f"The comparison supports {'keeping' if word == 'keep' else 'reverting'} this version."))
    names = {"corrections_per_outcome": "Corrections per outcome", "cost_per_outcome": "Cost per outcome"}
    descriptions = {"better": "improved", "worse": "got worse", "no_worse": "was no worse",
                    "unclear": "is unclear", "too_few": "needs more comparable data",
                    "unknown": "is not known", "not_comparable": "can't be compared",
                    "unconfirmed": "has unconfirmed values"}
    for call in calls or []:
        name = names.get(str(call.get("name")))
        description = descriptions.get(str(call.get("call")))
        if call.get("reason") == "cost_unknown":
            lines.append(REASON_COPY["cost_unknown"])
        elif name and description:
            lines.append(f"{name} {description}.")
    for reason in ("local_edit", "unversioned"):
        count = excluded.get(reason)
        if type(count) is int and count:
            if reason == "local_edit":
                lines.append(f"{count} {'run used' if count == 1 else 'runs used'} a workflow edit that wasn't saved as a version, so they aren't compared.")
            else:
                lines.append(f"{count} {'run has' if count == 1 else 'runs have'} no version recorded, so they aren't compared.")
    return list(dict.fromkeys(lines))
