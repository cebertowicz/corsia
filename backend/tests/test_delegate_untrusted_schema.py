"""The delegation surface exposes one name for untrusted confinement."""

from tools.delegate_task import DelegateTaskTool, _confinement_args


def test_confinement_enum_offers_only_none_and_untrusted():
    schema = DelegateTaskTool().parameters
    assert schema["properties"]["confinement"]["enum"] == ["none", "untrusted", None]
    assert _confinement_args({"confinement": "review", "read_paths": ["/src"]}) == (
        "Error: confinement must be one of none, untrusted."
    )
