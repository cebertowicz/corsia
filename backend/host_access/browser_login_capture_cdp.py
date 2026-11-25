"""CDP helpers for the saved-login capture's isolated page reads."""
from __future__ import annotations

import logging

from host_access import browser_cdp_frames
from host_access.browser_cdp import CdpClient, protocol_int
from host_access.browser_session_state import BrowserSessionError


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
