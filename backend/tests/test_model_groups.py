"""Model-row grouping keeps every observed model represented."""

from api.model_groups import ModelTally, split_by_spend


def test_split_by_spend_pools_zero_spend_models_below_the_task_floor():
    priced = ModelTally(ledger=1.0)
    local = ModelTally()

    kept, rest = split_by_spend({"priced": priced, "local": local}, minimum=14, shown=6)

    assert kept == ["priced"]
    assert rest == ["local"]
