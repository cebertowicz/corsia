"""Single-action browser tools that each wrap one agent-browser CLI command."""

from typing import Any

from host_access.browser_login_fill import MAX_MINUTES
from tools.base import Tool
from tools.browser_input import BrowserInputTool as BrowserInputTool
from tools.browser_runtime import _declare_person_step, _execution_context, _run


class BrowserSnapshotTool(Tool):
    """Get the accessibility tree of the current page."""

    name = "browser_snapshot"
    description = (
        "Get the accessibility tree of the current browser page. "
        "Returns semantic DOM with element refs (e.g. @e42) for interaction. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- See what's on the current page (text, buttons, inputs, links)\n"
        "- Find element refs for browser_click or browser_fill\n"
        "- Verify UI state after interactions\n\n"
        "Prefer this over browser_screenshot for understanding page structure — "
        "it's faster and more AI-friendly.\n\n"
        "Tabs: after a page opens a popup, or when the person needs a page that is not showing, set list_tabs "
        "to get each tab's ref, title, URL and whether it is active. Pass a ref as tab to make that tab active "
        "first: later browser calls act on it, and the shared pane shows it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "interactive_only": {
                "type": "boolean",
                "description": "Only show interactive elements (buttons, inputs, links). Default: false (show all)."
            },
            "list_tabs": {
                "type": "boolean",
                "description": "Return the open tabs instead of the page tree. Default: false."
            },
            "tab": {"type": "string", "description": "A tab ref from list_tabs to make active before the snapshot or tab list."},
        },
        "required": [],
    }

    async def execute(
        self, interactive_only: bool = False, list_tabs: bool = False, tab: str | None = None, **kwargs: Any,
    ) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        if tab is not None:
            if not tab or tab in {"new", "close", "list"} or tab.startswith("-") or any(c.isspace() for c in tab):
                return "Error: use a tab ref from list_tabs."
            switched = await _run(["tab", tab], thread_id=thread_id)
            if switched.startswith("Error"):
                return switched
        if list_tabs:
            return await _run(["tab"], thread_id=thread_id)
        args = ["snapshot"]
        if interactive_only:
            args.append("-i")
        return await _run(
            args,
            thread_id=thread_id,
            timeout=15,
        )



class BrowserClickTool(Tool):
    """Click an element in the browser."""

    name = "browser_click"
    description = (
        "Click an element in the browser by ref (from snapshot) or CSS selector. "
        "These actions work alongside the person. Never click what would show a password, key or code "
        "(such as creating or revealing a key) or accept a consent screen's request for account access: "
        "post_update the person to click it, then watch only the page's URL and title. Only a skill "
        "that saves the password, key or code unseen may have you click to show it yourself.\n\n"
        "USE THIS to:\n"
        "- Click buttons, links, checkboxes\n"
        "- Navigate between pages\n"
        "- Trigger UI actions\n\n"
        "Get element refs from browser_snapshot (e.g. @e42)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Element ref from snapshot (e.g. '@e42') or CSS selector"
            },
        },
        "required": ["selector"],
    }

    async def execute(self, selector: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["click", selector],
            thread_id=thread_id,
        )



class BrowserFillTool(Tool):
    """Fill a text input in the browser."""

    name = "browser_fill"
    description = (
        "Clear and fill a text input in the browser. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Fill form fields, search boxes, text areas\n"
        "- Enter test data\n"
        "- Sign in with a login the person saved: pass saved_login (and no selector or text) on the site's "
        "sign-in page. CORSIA asks the person to approve, then types it; you never see the password.\n"
        "- Fill another saved item (an API key, an account number): pass saved_item and the field's selector (no "
        "text). CORSIA asks the person to approve it for that site, then fills it in; you never see it.\n\n"
        "Get element refs from browser_snapshot (e.g. @e5)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Element ref from snapshot (e.g. '@e5') or CSS selector"
            },
            "text": {
                "type": "string",
                "description": "Text to fill into the element"
            },
            "saved_login": {
                "type": "object",
                "description": "A saved login to sign in with, instead of selector and text. If none matches, "
                               "the result lists the usernames you may use on this page's site.",
                "properties": {
                    "site": {"type": "string", "description": "The site's https address, e.g. 'https://example.com'"},
                    "username": {"type": "string"},
                },
                "required": ["site", "username"],
            },
            "saved_item": {
                "type": "string",
                "description": "The name of a saved item to fill into selector, instead of text. If none "
                               "matches, the result lists the items you may ask for.",
            },
            "minutes": {
                "type": "integer",
                "description": "With saved_login or saved_item: how long your task needs it, 1-"
                               f"{MAX_MINUTES} minutes. The person decides.",
            },
        },
        "required": [],
    }

    async def execute(self, selector: str | None = None, text: str | None = None, saved_login: Any = None,
                      minutes: Any = None, saved_item: Any = None, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        if saved_item is not None:
            import tools.browser_saved_login as browser_saved_login

            if not isinstance(selector, str) or not selector or text is not None or saved_login is not None:
                return browser_saved_login.ITEM_EXCLUSIVE
            return await browser_saved_login.fill_item(thread_id, saved_item, selector, minutes)
        if saved_login is not None or minutes is not None:
            import tools.browser_saved_login as browser_saved_login

            if selector is not None or text is not None:
                return browser_saved_login.EXCLUSIVE
            return await browser_saved_login.fill(thread_id, saved_login, minutes)
        if selector is None or text is None:
            return "Error: browser_fill needs selector and text, saved_login, or selector and saved_item."
        return await _run(
            ["fill", selector, text],
            thread_id=thread_id,
        )



class BrowserEvalTool(Tool):
    """Run JavaScript in the browser and return the result."""

    name = "browser_eval"
    description = (
        "Run JavaScript in the current browser page and return the result. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Extract data from the page (e.g. document.title, localStorage)\n"
        "- Check computed state or values\n"
        "- Perform actions not possible through the accessibility tree\n\n"
        "Note: Do NOT use 'return' — the last expression is the result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript to evaluate. Last expression is the return value. No 'return' keyword."
            },
        },
        "required": ["script"],
    }

    async def execute(self, script: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["eval", script],
            thread_id=thread_id,
        )




class BrowserPersonStepTool(Tool):
    """Let the person complete a visible browser page and wake the agent after navigation."""

    name = "browser_person_step"
    description = (
        "Give the current browser page to the person to complete, then end your turn. "
        "CORSIA puts the step in their Needs you list, which opens the page in their CORSIA pane, and wakes you "
        "when the page changes. The reply says whether their pane is already showing the page. "
        "Use this for sign-in, account choice, verification, passkeys, or phone prompts; never ask for or "
        "read a password or code."
    )
    parameters = {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The short action the person should complete, such as 'Sign in to Google'.",
            },
        },
        "required": ["instruction"],
    }

    async def execute(self, instruction: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _declare_person_step(thread_id, instruction)


class BrowserWaitTool(Tool):
    """Wait for an element to appear or a fixed time."""

    name = "browser_wait"
    description = (
        "Wait for an element to appear on the page, or wait a fixed number of milliseconds. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Wait for async content to load after an action\n"
        "- Pause between interactions\n"
        "- Wait for navigation to complete\n\n"
        "Pass a CSS selector or @ref to wait for an element, or a number (ms) to wait a fixed time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Element selector/ref to wait for (e.g. '@e5', '.my-class'), OR milliseconds as string (e.g. '1000')"
            },
        },
        "required": ["target"],
    }

    async def execute(self, target: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["wait", target],
            thread_id=thread_id,
            timeout=30,
        )

