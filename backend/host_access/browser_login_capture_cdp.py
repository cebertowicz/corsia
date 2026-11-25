"""CDP helpers for the saved-login capture's isolated page reads."""
from __future__ import annotations

import logging

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, protocol_int
from host_access.browser_session_state import BrowserSessionError


# The isolated-world read of a password field and a username beside it, as [typed, username]; null when the node
# isn't an input of this world's document. Username: an autocomplete~=username input in the field's form (or the
# document), else a type=email one, else the last visible text, email or tel input before the field.
READ_LOGIN = """function() {
  if (!(this instanceof HTMLInputElement) || this.ownerDocument !== document) return null;
  const field = this;
  const inputs = Array.from((field.form || document).querySelectorAll('input')).filter(el => el !== field);
  const tokens = el => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
  const shows = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const before = inputs.filter(el => ['text', 'email', 'tel'].includes(el.type) && shows(el)
    && (el.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING));
  const account = inputs.find(el => tokens(el).includes('username'))
    || inputs.find(el => el.type === 'email') || before[before.length - 1];
  return [field.value, account ? account.value : ''];
}"""
# The isolated-world read of a username the person filled in: the focused input, else one in the focused
# element's form, that is type=email or autocomplete~=username/email and not empty; null otherwise.
READ_ACCOUNT = """(() => {
  const tokens = el => (el.getAttribute('autocomplete') || '').toLowerCase().split(/\\s+/);
  const named = el => el instanceof HTMLInputElement && el.value !== ''
    && (el.type === 'email' || tokens(el).includes('username') || tokens(el).includes('email'));
  const active = document.activeElement;
  if (named(active)) return active.value;
  const found = active && active.form ? Array.from(active.form.elements).find(named) : undefined;
  return found ? found.value : null;
})()"""


class _Silent(logging.Logger):
    """Disable websocket frame logs for isolated login reads."""

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging.Logger's own name
        return False


def quiet_cdp(endpoint: str) -> CdpClient:
    """Use the shared client without logging sensitive frames."""
    return CdpClient(endpoint, logger=_Silent("corsia-login-capture"))


async def frame_tree(cdp: CdpClient) -> tuple[str, list[tuple[str, str]]]:
    """The main document's loader and reachable frame ids with their security origins."""
    tree = (await cdp.call("Page.getFrameTree")).get("frameTree") or {}
    loader = (tree.get("frame") or {}).get("loaderId")
    frames: list[tuple[str, str]] = []
    pending = [tree]
    while pending and len(frames) < browser_cdp_frames.FRAME_LIMIT:
        node = pending.pop(0)
        frame = node.get("frame") or {}
        if isinstance(frame.get("id"), str):
            frames.append((frame["id"], str(frame.get("securityOrigin") or "")))
        pending.extend(child for child in node.get("childFrames") or [] if isinstance(child, dict))
    return (loader if isinstance(loader, str) else ""), frames


async def world(cdp: CdpClient, frame_id: str, name: str) -> int | None:
    """A fresh isolated world in the frame, or None if this target cannot reach it."""
    try:
        result = await cdp.call("Page.createIsolatedWorld", frameId=frame_id, worldName=name, grantUniveralAccess=False)
        return protocol_int(result.get("executionContextId"))
    except BrowserSessionError:
        return None


async def call_on(cdp: CdpClient, node: int, context: int | None, declaration: str) -> tuple[str, str] | None:
    """Read the node in its isolated world; None if it is not in that document."""
    if context is None:
        return None
    try:
        resolved = await cdp.call("DOM.resolveNode", backendNodeId=node, executionContextId=context)
        object_id = (resolved.get("object") or {}).get("objectId")
        if not isinstance(object_id, str):
            return None
        outcome = await cdp.call("Runtime.callFunctionOn", objectId=object_id, functionDeclaration=declaration,
                                 returnByValue=True, silent=True)
    except BrowserSessionError:
        return None
    value = (outcome.get("result") or {}).get("value") if "exceptionDetails" not in outcome else None
    if isinstance(value, list) and len(value) == 2 and all(isinstance(item, str) for item in value):
        return value[0], value[1]
    return None
