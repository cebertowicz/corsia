import pytest

from host_access import browser_cdp


class Cdp:
    def __init__(self, *, readonly=False, disabled=False, reveal=False, form=False, credentials=False):
        self.nodes = {
            5: {"nodeName": "INPUT", "nodeId": 5, "backendNodeId": 77, "parentId": 6,
                "attributes": ["type", "password"] + (["value", "fixture"] if form else [])
                + (["readonly", ""] if readonly else []) + (["disabled", ""] if disabled else [])},
            6: {"nodeName": "DIV", "nodeId": 6, "parentId": 7, "attributes": []},
            7: {"nodeName": "FORM" if form else "BODY", "nodeId": 7, "parentId": 8, "attributes": []},
            8: {"nodeName": "#document", "nodeId": 8},
        }
        self.parents = {5: 6, 6: 7, 7: 8}
        self.reveal, self.form, self.credentials = reveal, form, credentials

    async def call(self, method, **params):
        if method == "DOM.describeNode":
            return {"node": {k: v for k, v in self.nodes[params["nodeId"]].items() if k != "parentId"}}
        if method == "Accessibility.queryAXTree":
            return {"nodes": [{"role": {"value": "button"}, "name": {"value": "Show secret"}}] if self.reveal else []}
        if method == "DOM.querySelectorAll":
            selector = params["selector"]
            if "one-time-code" in selector:
                return {"nodeIds": [10] if self.credentials else []}
            return {"nodeIds": [11] if self.form else []}
        return {
            "DOM.getDocument": {},
            "DOM.performSearch": {"searchId": "search", "resultCount": 1},
            "DOM.getSearchResults": {"nodeIds": [5]},
            "DOM.discardSearchResults": {},
        }[method]


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"readonly": True}, {"disabled": True}, {"reveal": True}],
                         ids=["readonly", "disabled", "reveal-sibling"])
async def test_display_secret_is_not_a_password_prompt(options):
    assert await browser_cdp._password_inputs(Cdp(**options)) == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"form": True, "reveal": True}, {"credentials": True, "reveal": True}, {}],
                         ids=["prefilled-form", "nearby-username", "empty-sign-in"])
async def test_sign_in_password_is_guarded(options):
    assert await browser_cdp._password_inputs(Cdp(**options)) == {77}


@pytest.mark.asyncio
async def test_slack_box_reveal_text_in_wrapper_is_not_a_prompt():
    class SlackBox(Cdp):
        def __init__(self):
            super().__init__()
            self.parents[6] = 9
            self.parents[9] = 7
            self.nodes[9] = {"nodeName": "DIV", "nodeId": 9, "parentId": 7, "attributes": []}
            self.nodes[5]["attributes"] += ["value", "fixture"]

        async def call(self, method, **params):
            if method == "Accessibility.queryAXTree":
                return {"nodes": ([{"role": {"value": "button"}, "name": {"value": "Show"}},
                                    {"role": {"value": "button"}, "name": {"value": "Regenerate"}}]
                                   if params["nodeId"] == 9 else [])}
            return await super().call(method, **params)

    assert await browser_cdp._password_inputs(SlackBox()) == set()


@pytest.mark.asyncio
async def test_readonly_password_in_sign_in_form_remains_guarded():
    assert await browser_cdp._password_inputs(Cdp(readonly=True, form=True)) == {77}


@pytest.mark.asyncio
async def test_missing_pushed_ancestry_keeps_field_guarded():
    cdp = Cdp(readonly=True)
    cdp.parents.clear()
    assert await browser_cdp._password_inputs(cdp) == {77}
