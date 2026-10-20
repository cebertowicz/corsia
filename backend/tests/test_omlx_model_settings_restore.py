"""The model-settings writer over real HTTP: a restore's null reaches oMLX, and a missing model is told apart.

Upstream oMLX's ``PUT /admin/api/models/{model_id}/settings`` (github.com/jundot/omlx,
tag v0.7.0, ``omlx/admin/routes.py`` 2714-2752) clears a field sent as null to its
default (``model_fields_set``) and leaves an omitted one alone, and answers 404
``Model not found: {model_id}`` for a model it doesn't have.
"""

from __future__ import annotations

import asyncio

from llm.providers.omlx_settings import update_model_settings
from omlx_admin_fixtures import CFG, omlx  # noqa: F401

PATH = "/admin/api/models/their-model/settings"


def test_a_restore_sends_each_unset_value_as_null_and_nothing_else(omlx):  # noqa: F811
    result = asyncio.run(update_model_settings(CFG, "their-model", {"max_tokens": None, "mtp_enabled": None,
                                                                    "max_context_window": 32768}))

    assert result["ok"] is True
    [body] = [body for method, path, body in omlx.bodies if (method, path) == ("PUT", PATH)]
    assert body == {"max_tokens": None, "mtp_enabled": None, "max_context_window": 32768}


def test_a_model_oMLX_no_longer_has_reads_as_missing(omlx):  # noqa: F811
    omlx.missing_models.add("their-model")

    result = asyncio.run(update_model_settings(CFG, "their-model", {"max_tokens": None}))

    assert (result["ok"], result["status"], result["model_missing"]) == (False, 404, True)


def test_any_other_failure_is_not_a_missing_model(omlx):  # noqa: F811
    omlx.no_model_route = True

    result = asyncio.run(update_model_settings(CFG, "their-model", {"max_tokens": None}))

    assert (result["ok"], result["status"], result["model_missing"]) == (False, 404, False)
