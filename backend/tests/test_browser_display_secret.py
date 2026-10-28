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
        self.reveal, self.form, self.credentials = reveal, form, credentials

    async def call(self, method, **params):
        if method == "DOM.describeNode":
            return {"node": self.nodes[params["nodeId"]]}
        if method == "DOM.querySelectorAll":
            selector = params["selector"]
            if "aria-label" in selector:
                return {"nodeIds": [9] if self.reveal else []}
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
