"""The page half of a saved-login fill: find the sign-in form, check where it sends, type into it, submit it.

browser_login_fill decides whether to fill and reads the saved value; this module never names a saved-login
reader and receives the value only as an argument to `type_values`, where it lives in locals and in one CDP
message. Every connection here is a QuietCdp: its socket logs nothing and its errors carry no protocol text,
so neither a frame nor an error message can hold the value. All page work runs in an isolated world (C5), so
no page script sees the elements chosen or the value except through the field itself, and every DOM property
read goes through the prototype's own getter, so an element named "action" inside a form can't stand in for
the form's action. Everything this module returns is a fixed status, never page text.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import websockets

from host_access.browser_cdp import CdpClient
from host_access.browser_domain import registrable_domain
from host_access.browser_session_state import BrowserSessionError

PAGE_CHECK_FAILED = "This page couldn't be checked"
_WORLD = "corsia-saved-login"
# Request kinds that carry a sign-in; the rest (images, styles, long-lived streams) don't decide when it settled.
_SIGN_IN_REQUESTS = frozenset({"Document", "XHR", "Fetch", "Other"})
# A request still open this long after it started is a page's background traffic, not the sign-in.
BACKGROUND_AFTER = 5.0


class _Silent(logging.Logger):
    """A logger that records nothing, built outside the logging tree so no configuration can turn it on."""

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802 - logging's own name
        return False

    def handle(self, record: logging.LogRecord) -> None:
        return None


_SILENT = _Silent("corsia.saved-login-socket")


class QuietCdp(CdpClient):
    """A CDP connection for a fill: no frame logging, and failures with fixed text only."""

    async def __aenter__(self) -> QuietCdp:
        self.socket = await websockets.connect(self.endpoint, max_size=self._max_size, open_timeout=3,
                                               close_timeout=1, logger=_SILENT)
        return self

    async def call_in(self, session_id: str | None, method: str, **params: object) -> dict:
        request_id = await self.send(method, session_id=session_id, **params)
        while True:
            response = json.loads(await self.socket.recv())
            if response.get("id") != request_id:
                self._event(response)
                continue
            result = response.get("result")
            if "error" in response or not isinstance(result, dict):
                raise BrowserSessionError(PAGE_CHECK_FAILED)
            return result


# Shared by every script below: the prototypes' own getters, and the submit button and action of a form.
_HELPERS = r"""
const own = (proto, name) => Object.getOwnPropertyDescriptor(proto, name).get;
const I = HTMLInputElement.prototype, B = HTMLButtonElement.prototype, F = HTMLFormElement.prototype;
const type = own(I, 'type'), owner = own(I, 'form'), elements = own(F, 'elements'), formAction = own(F, 'action');
const submitter = form => form && [...elements.call(form)].find(el =>
  el instanceof HTMLButtonElement ? own(B, 'type').call(el) === 'submit'
    : el instanceof HTMLInputElement && ['submit', 'image'].includes(type.call(el))) || null;
const action = form => {
  if (!form) return '';
  const button = submitter(form);
  if (button && button.hasAttribute('formaction'))
    return own(button instanceof HTMLButtonElement ? B : I, 'formAction').call(button);
  return formAction.call(form);
};
"""

# Finds the one visible password field in this frame and keeps it, its form and its username field in this
# isolated world. Returns counts and the resolved action URL only.
_FIND = "(() => {" + _HELPERS + r"""
const shown = el => { const box = el.getBoundingClientRect(), style = getComputedStyle(el);
  return box.width > 0 && box.height > 0 && style.visibility === 'visible' && style.display !== 'none'; };
const inputs = root => { const found = [...root.querySelectorAll('input')];
  for (const host of root.querySelectorAll('*')) if (host.shadowRoot) found.push(...inputs(host.shadowRoot));
  return found; };
globalThis.__corsiaFill = null;
const passwords = inputs(document).filter(el => type.call(el) === 'password' && shown(el));
if (passwords.length !== 1) return {passwords: passwords.length};
const field = passwords[0], form = owner.call(field);
const scope = form ? [...elements.call(form)].filter(el => el instanceof HTMLInputElement) : inputs(field.getRootNode());
const usable = scope.filter(el => ['text', 'email'].includes(type.call(el)) && shown(el) && !el.disabled && !el.readOnly);
const named = el => /(^|\s)username(\s|$)/i.test(el.getAttribute('autocomplete') || '');
const before = usable.filter(el => field.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING);
const user = usable.find(named) || usable.find(el => type.call(el) === 'email') || before[before.length - 1] || null;
globalThis.__corsiaFill = {field, form, user, action: action(form)};
return {passwords: 1, form: !!form, user: !!user, action: globalThis.__corsiaFill.action};
})()"""

# Types the username, then the password, each only while its field is still what was checked.
_TYPE = "function (user, text) {" + _HELPERS + r"""
const kept = globalThis.__corsiaFill;
const same = () => kept && kept.field.isConnected && type.call(kept.field) === 'password'
  && owner.call(kept.field) === kept.form && action(kept.form) === kept.action;
const set = Object.getOwnPropertyDescriptor(I, 'value').set;
const put = (el, value, check) => {
  el.focus();
  if (!check()) return false;
  set.call(el, value);
  for (const name of ['input', 'change']) el.dispatchEvent(new Event(name, {bubbles: true}));
  return true;
};
if (!same()) return 'changed';
if (kept.user && user !== null && kept.user.isConnected)
  put(kept.user, user, () => ['text', 'email'].includes(type.call(kept.user)));
return put(kept.field, text, same) ? 'typed' : 'changed';
}"""

# Submits the way the form does: Enter in a focused top-frame field, else the form's own submit button.
_SUBMIT = "function () {" + _HELPERS + r"""
const kept = globalThis.__corsiaFill;
if (!kept || !kept.field.isConnected || action(kept.form) !== kept.action) return 'changed';
kept.field.focus();
if (window === window.top && kept.field.getRootNode().activeElement === kept.field) return 'enter';
const button = submitter(kept.form);
if (button && !button.disabled) { HTMLElement.prototype.click.call(button); return 'clicked'; }
return 'unsubmitted';
}"""


@dataclass(frozen=True)
class SignInForm:
    """Where the checked field is: its frame's isolated world, and whether that frame is the top one."""

    context: int
    top: bool


def frames(tree: dict) -> list[tuple[dict, bool]]:
    """Every frame in a Page.getFrameTree result, top first, each with whether it is the top frame."""
    found, pending = [], [(tree.get("frameTree") or {}, True)]
    while pending:
        node, top = pending.pop(0)
        frame = node.get("frame")
        if isinstance(frame, dict):
            found.append((frame, top))
        pending.extend((child, False) for child in node.get("childFrames") or [] if isinstance(child, dict))
    return found


async def main_loader(cdp: CdpClient) -> str:
    """The top document's loader id; empty when it can't be read."""
    loader = ((await cdp.call("Page.getFrameTree")).get("frameTree") or {}).get("frame", {}).get("loaderId")
    return loader if isinstance(loader, str) else ""


async def find_form(cdp: CdpClient, site_origin: str, normalize) -> SignInForm | str:
    """The one visible password field on the page, checked for C5 and C7; a refusal key when there isn't one.

    `normalize` is saved_logins.normalized_site; the field's frame origin comes from Page.getFrameTree.
    """
    found: list[tuple[dict, bool, int, dict]] = []
    for frame, top in frames(await cdp.call("Page.getFrameTree")):
        try:
            world = await cdp.call("Page.createIsolatedWorld", frameId=frame.get("id"), worldName=_WORLD,
                                   grantUniveralAccess=False)
            context = world.get("executionContextId")
            answer = await _value(cdp, "Runtime.evaluate", expression=_FIND, contextId=context, returnByValue=True)
        except BrowserSessionError:
            continue  # A frame in another process isn't reached from this page; its field isn't filled.
        if isinstance(answer, dict) and isinstance(answer.get("passwords"), int) and answer["passwords"]:
            found.append((frame, top, context, answer))
    if sum(answer["passwords"] for *_, answer in found) != 1:
        return "no_field"
    frame, top, context, answer = found[0]
    origin = https_url(frame.get("securityOrigin"))
    if origin is None:
        return "http_frame"
    try:
        if normalize(origin) != site_origin:
            return "frame_origin"
    except ValueError:
        return "frame_origin"
    if answer.get("form") and not _same_site(answer.get("action"), site_origin):
        return "form_action"
    return SignInForm(context, top)


def https_url(url: object) -> str | None:
    """The URL when it is https, else None: the one https check of a fill (a test admits its http fixture here)."""
    return url if isinstance(url, str) and url.startswith("https://") else None


def _same_site(action: object, site_origin: str) -> bool:
    """Whether a form's resolved action is https on the saved site's registrable domain."""
    action = https_url(action)
    if action is None:
        return False
    try:
        host, site = urlsplit(action).hostname, urlsplit(site_origin).hostname
    except ValueError:
        return False
    return bool(host) and registrable_domain(host or "") == registrable_domain(site or "")


async def type_values(cdp: CdpClient, form: SignInForm, username: str | None, text: str) -> str:
    """Type the username and the saved value into the checked fields: 'typed', or 'changed' when they moved."""
    status = await _value(cdp, "Runtime.callFunctionOn", functionDeclaration=_TYPE, executionContextId=form.context,
                          arguments=[{"value": username}, {"value": text}], returnByValue=True)
    return status if status in {"typed", "changed"} else "changed"


async def submit(cdp: CdpClient, form: SignInForm) -> str:
    """Submit the form the way it submits: 'enter', 'clicked', 'changed' or 'unsubmitted'."""
    status = await _value(cdp, "Runtime.callFunctionOn", functionDeclaration=_SUBMIT,
                          executionContextId=form.context, returnByValue=True)
    if status == "enter" and not form.top:
        status = "unsubmitted"
    if status == "enter":
        for kind in ("keyDown", "keyUp"):
            await cdp.call("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter",
                           windowsVirtualKeyCode=13, **({"text": "\r"} if kind == "keyDown" else {}))
    return status if status in {"enter", "clicked", "changed", "unsubmitted"} else "unsubmitted"


async def _value(cdp: CdpClient, method: str, **params: object) -> object:
    result = await cdp.call(method, **params)
    if result.get("exceptionDetails") is not None:
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    return (result.get("result") or {}).get("value")


async def linked_windows(browser: CdpClient, target_id: str) -> bool:
    """Whether the page can reach, or be reached by, another window through an opener (a popup's script)."""
    targets = (await browser.call("Target.getTargets")).get("targetInfos") or []
    pages = [info for info in targets if isinstance(info, dict) and info.get("type") == "page"]
    return any(info.get("targetId") == target_id and info.get("canAccessOpener") or
               info.get("openerId") == target_id and info.get("canAccessOpener") for info in pages)


class Requests:
    """The page's sign-in requests since the fill began, by id and start time only, never URL or body."""

    def __init__(self) -> None:
        self.seen = 0
        self._open: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._cdp: QuietCdp | None = None

    def in_flight(self, now: float) -> bool:
        return any(now - started < BACKGROUND_AFTER for started in self._open.values())

    def note(self, message: dict, now: float) -> None:
        method, params = message.get("method"), message.get("params") or {}
        request = params.get("requestId") if isinstance(params, dict) else None
        if not isinstance(request, str):
            return
        if method == "Network.requestWillBeSent" and params.get("type") in _SIGN_IN_REQUESTS:
            self.seen += 1
            self._open[request] = now
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            self._open.pop(request, None)

    async def start(self, endpoint: str) -> None:
        self._cdp = await QuietCdp(endpoint).__aenter__()
        await self._cdp.call("Network.enable", maxPostDataSize=0, maxResourceBufferSize=0, maxTotalBufferSize=0)
        self._task = asyncio.create_task(self._read(self._cdp))

    async def _read(self, cdp: QuietCdp) -> None:
        clock = asyncio.get_running_loop().time
        try:
            async for frame in cdp.socket:
                message = json.loads(frame)
                self.note(message, clock())
                del message, frame
        except (websockets.WebSocketException, OSError, ValueError):
            return

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._cdp is not None:
            await self._cdp.__aexit__()
