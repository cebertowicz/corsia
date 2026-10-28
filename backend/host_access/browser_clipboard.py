"""Native clipboard commands for the shared browser page."""
import asyncio
import json
import sys
from urllib.parse import urlsplit

from host_access.browser_cdp import CdpClient
from host_access.browser_session_state import BrowserSessionError

WORLD = "corsia-clipboard"
# The listener and its record live in an isolated world, never in page script state.
PRELOAD = """(() => {
 if (window.__corsiaClipboardListener) return;
 window.__corsiaClipboardListener = true;
 for (const kind of ['copy', 'cut']) {
  const pending = new WeakMap();
  window.addEventListener(kind, event => {
   if (!event.isTrusted) return;
   let owner;
   try { owner = window.top; void owner.document; } catch { owner = window; }
   let node = document.activeElement;
   while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement;
   let selection = '';
   if (node instanceof HTMLInputElement || node instanceof HTMLTextAreaElement) {
    if (node.type !== 'password' && node.selectionStart !== null && node.selectionEnd !== null)
     selection = node.value.slice(node.selectionStart, node.selectionEnd);
   } else selection = window.getSelection()?.toString() || '';
   const record = {kind, selection, data: ''};
   pending.set(event, record);
   setTimeout(() => {
    record.prevented = event.defaultPrevented;
    owner.__corsiaClipboardRecord = record;
    owner.__corsiaClipboardSequence = (owner.__corsiaClipboardSequence || 0) + 1;
   }, 0);
  }, true);
  window.addEventListener(kind, event => {
   const record = pending.get(event);
   if (record) record.data = event.clipboardData?.getData('text/plain') || '';
  });
 }
})()"""


class PageHandledError(BrowserSessionError):
    """A page handled the event but its custom clipboard data was inaccessible."""


async def _world(cdp: CdpClient) -> tuple[int, str]:
    tree = await cdp.call("Page.getFrameTree")
    frame = tree.get("frameTree", {}).get("frame", {})
    frame_id, url = frame.get("id"), frame.get("url")
    if not isinstance(frame_id, str) or not isinstance(url, str):
        raise BrowserSessionError("This page couldn't receive clipboard input")
    result = await cdp.call("Page.createIsolatedWorld", frameId=frame_id, worldName=WORLD)
    context = result.get("executionContextId")
    if not isinstance(context, int):
        raise BrowserSessionError("This page couldn't receive clipboard input")
    return context, url


async def _evaluate(cdp: CdpClient, context: int, expression: str) -> object:
    result = await cdp.call("Runtime.evaluate", expression=expression, contextId=context,
                            returnByValue=True, awaitPromise=True)
    if result.get("exceptionDetails"):
        raise BrowserSessionError("This page couldn't receive clipboard input")
    return result.get("result", {}).get("value")


async def _command(cdp: CdpClient, command: str) -> None:
    key = {"copy": "c", "cut": "x", "paste": "v", "undo": "z"}[command]
    args = {"key": key, "code": f"Key{key.upper()}", "windowsVirtualKeyCode": ord(key.upper()), "modifiers": 4 if sys.platform == "darwin" else 2}
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", commands=[command], **args)
    await cdp.call("Input.dispatchKeyEvent", type="keyUp", **args)


async def clipboard_selection(endpoint: str, cut: bool) -> str:
    """Dispatch native copy or cut and retrieve the isolated event record."""
    async with CdpClient(endpoint, max_size=30_004_096) as cdp:
        context, _ = await _world(cdp)
        sequence = await _evaluate(cdp, context, "window.__corsiaClipboardSequence || 0")
        if not isinstance(sequence, int):
            raise BrowserSessionError("This page couldn't be copied")
        kind = "cut" if cut else "copy"
        await _command(cdp, kind)
        for _ in range(20):
            record = await _evaluate(cdp, context, f"(() => {{ if (window.__corsiaClipboardSequence <= {sequence}) return null; const record = window.__corsiaClipboardRecord; delete window.__corsiaClipboardRecord; return record; }})()")
            if isinstance(record, dict) and record.get("kind") == kind:
                if record.get("prevented"):
                    text = record.get("data")
                    if not text:
                        raise PageHandledError("The page handled this clipboard action")
                else:
                    text = record.get("selection")
                if isinstance(text, str):
                    return text
                break
            await asyncio.sleep(0.025)
        raise BrowserSessionError("This page couldn't be copied")


async def clipboard_paste(endpoint: str, text: str) -> None:
    """Write the person's text with a temporary write-only grant and paste natively."""
    async with CdpClient(endpoint) as cdp:
        context, url = await _world(cdp)
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            raise BrowserSessionError("This page couldn't receive pasted text")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        await cdp.call("Browser.grantPermissions", origin=origin, permissions=["clipboardSanitizedWrite"])
        try:
            await _evaluate(cdp, context, f"navigator.clipboard.writeText({json.dumps(text)})")
        finally:
            await cdp.call("Browser.resetPermissions")
        await _command(cdp, "paste")


async def clipboard_undo(endpoint: str) -> None:
    """Ask the focused page to undo the last native edit."""
    async with CdpClient(endpoint) as cdp:
        await _command(cdp, "undo")
