"""Clipboard operations on the shared browser page."""
import json

from websockets.exceptions import ConnectionClosed, PayloadTooBig, WebSocketException

from host_access.browser_cdp import CdpClient
from host_access.browser_session_state import BrowserSessionError


class FrameSelectionError(BrowserSessionError):
    """Focus is inside an embedded frame, whose selection the top document cannot read."""


class SelectionTooLargeError(BrowserSessionError):
    """The selected text exceeded the CDP response limit."""


# Six bytes per selected byte covers JSON escaping, plus room for the CDP envelope.
_SELECTION_MAX_SIZE = 6 * 5_000_000 + 4096

# Descend through the focused document and shadow roots; a cross-origin frame is inaccessible.
_FOCUSED_DOCUMENT = """let doc = document, node;
 while (true) {
  node = doc.activeElement;
  while (node?.shadowRoot?.activeElement) node = node.shadowRoot.activeElement;
  if (node?.tagName !== 'IFRAME' && node?.tagName !== 'FRAME') break;
  try { doc = node.contentDocument; } catch { return null; }
  if (!doc) return null;
 }
 const view = doc.defaultView;"""

# Read only the person's selected text. Password controls deliberately retain Chrome's no-copy behavior.
_SELECTION = """(() => {
 """ + _FOCUSED_DOCUMENT + """
 if (node instanceof view.HTMLInputElement || node instanceof view.HTMLTextAreaElement) {
  if (node.type === 'password' || node.selectionStart === null || node.selectionEnd === null) return '';
  window.__corsiaCopiedSelection = {node, doc, start: node.selectionStart, end: node.selectionEnd};
  return node.value.slice(node.selectionStart, node.selectionEnd);
 }
 const selection = view.getSelection();
 window.__corsiaCopiedSelection = selection?.rangeCount ? {node, doc, range: selection.getRangeAt(0).cloneRange()} : null;
 return selection?.toString() || '';
})()"""


async def clipboard_selection(endpoint: str, request: str) -> str:
    """Read the page selection for a person's explicit copy or cut."""
    try:
        async with CdpClient(endpoint, max_size=_SELECTION_MAX_SIZE) as cdp:
            result = await cdp.call("Runtime.evaluate", expression=f"window.__corsiaCopyRequest = {json.dumps(request)}; {_SELECTION}", returnByValue=True)
    except WebSocketException as exc:
        if (isinstance(exc, PayloadTooBig) or isinstance(exc.__cause__, PayloadTooBig)
                or (isinstance(exc, ConnectionClosed) and exc.sent is not None and exc.sent.code == 1009)):
            raise SelectionTooLargeError("The selected text is too large") from exc
        raise
    value = result.get("result", {}).get("value")
    if not result.get("exceptionDetails") and value is None:
        raise FrameSelectionError("This page's selection is inside an embedded frame")
    if result.get("exceptionDetails") or not isinstance(value, str):
        raise BrowserSessionError("This page's selection couldn’t be copied")
    return value


async def clipboard_paste(endpoint: str, text: str) -> None:
    """Insert the person's clipboard into the focused page field."""
    async with CdpClient(endpoint) as cdp:
        await cdp.call("Input.insertText", text=text)


async def clipboard_cut(endpoint: str, text: str, request: str) -> None:
    """Delete only if the same selection remains in an editable field after the copy succeeded."""
    expected = json.dumps(text)
    expression = f"""(() => {{
 const copied = window.__corsiaCopiedSelection;
 if (window.__corsiaCopyRequest !== {json.dumps(request)}) return false;
 window.__corsiaCopiedSelection = null;
 window.__corsiaCopyRequest = null;
 {_FOCUSED_DOCUMENT}
 if (!copied || copied.doc !== doc || copied.node !== node || !node) return false;
 const field = node instanceof view.HTMLInputElement || node instanceof view.HTMLTextAreaElement;
 const selection = view.getSelection();
 const range = !field && selection?.rangeCount ? selection.getRangeAt(0) : null;
 const sameRange = field
  ? node.selectionStart === copied.start && node.selectionEnd === copied.end
  : range && copied.range && range.startContainer === copied.range.startContainer
    && range.startOffset === copied.range.startOffset && range.endContainer === copied.range.endContainer
    && range.endOffset === copied.range.endOffset;
 const selected = field ? node.value.slice(node.selectionStart, node.selectionEnd) : selection?.toString();
 if (!sameRange || selected !== {expected} || !selected) return false;
 const editable = (node instanceof view.HTMLInputElement || node instanceof view.HTMLTextAreaElement)
  ? !node.readOnly && !node.disabled && node.type !== 'password' : node.isContentEditable;
 return !editable || doc.execCommand('delete');
}})()"""
    async with CdpClient(endpoint) as cdp:
        result = await cdp.call("Runtime.evaluate", expression=expression, returnByValue=True)
    if result.get("exceptionDetails") or result.get("result", {}).get("value") is not True:
        raise BrowserSessionError("This page's selection couldn’t be cut")
