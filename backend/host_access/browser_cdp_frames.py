"""Out-of-process (cross-origin) frames for the password probe: their CDP sessions and node keys.

A cross-origin frame runs in its own renderer, so the page target's DOM search never reaches it. The probe
auto-attaches to each one on its own connection and searches its document as a flattened child session.
Node ids restart in every renderer, so a node inside a frame is keyed by its document and its own backendNodeId:
the key of a top-document node is its backendNodeId, and a frame's document is keyed by its frame element's key
and a tag of the document's loader id. A navigation inside the frame commits a new loader, so the nodes known in
the document it left never match a node of the new one. `frame_owner` recovers the top-document frame element the
read guard's hit test lands on for any node inside that frame.
"""
from __future__ import annotations

import zlib
from typing import Awaitable, Callable, Protocol

from host_access.browser_session_state import BrowserSessionError

_NODE_BITS = 32
# A page with more frames than this is not checked through; the page check fails closed.
FRAME_LIMIT = 64
PAGE_CHECK_FAILED = "This page couldn't be checked"


class Session(Protocol):
    """A CDP target session: the page's own connection or a frame's flattened child session."""

    session_id: str | None
    parents: dict[int, int]

    async def call(self, method: str, **params: object) -> dict: ...


class Attaching(Protocol):
    """The connection the probe owns: it routes child sessions and records the frames attached to them."""

    attached: list[dict]

    async def call_in(self, session_id: str | None, method: str, **params: object) -> dict: ...

    def session_parents(self, session_id: str) -> dict[int, int]: ...


class Page(Session, Attaching, Protocol):
    """The page's own connection: the top document's session, which also carries every frame's session."""


# Each document by its key (None: the top document), with whether every frame element above it shows.
Documents = dict[int | None, tuple[Session, bool]]


class FrameSession:
    """One out-of-process frame's flattened session, carried on the probe's own connection."""

    def __init__(self, client: Attaching, session_id: str) -> None:
        self.client = client
        self.session_id: str | None = session_id
        self.parents = client.session_parents(session_id)

    async def call(self, method: str, **params: object) -> dict:
        return await self.client.call_in(self.session_id, method, **params)


def node_key(document: int | None, node: object) -> int:
    """The probe's key for `node`, a backendNodeId in `document` (a `document_key`; None: the top document)."""
    if not isinstance(node, int) or isinstance(node, bool) or not 0 < node < 1 << _NODE_BITS:
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    return node if document is None else document << _NODE_BITS | node


def document_key(element: int, loader: object) -> int:
    """The key of the document loaded by `loader` in the frame whose element has the key `element`."""
    if not isinstance(loader, str) or not loader:
        raise BrowserSessionError(PAGE_CHECK_FAILED)
    return element << _NODE_BITS | zlib.crc32(loader.encode())


def split_key(key: int) -> tuple[int | None, int]:
    """A key's document key (None: the top document) and its backendNodeId in that document."""
    document = key >> _NODE_BITS
    return (document or None), key & ((1 << _NODE_BITS) - 1)


def frame_owner(key: int) -> int:
    """The top-document element holding a key's frame; a top-document node is its own."""
    # Each frame level adds a loader tag and a backendNodeId, so a level is two node widths.
    while key >> _NODE_BITS:
        key >>= 2 * _NODE_BITS
    return key


async def search_page(page: Page, find: Callable[[Session], Awaitable[set[int]]],
                      visible: Callable[[Session, int], Awaitable[bool]]) -> tuple[set[int], Documents]:
    """`find`'s nodes in the page and every frame inside it, keyed, with each document's session."""
    found: set[int] = set()
    documents: Documents = {None: (page, True)}
    pending: list[int | None] = [None]
    while pending:
        document = pending.pop()
        session, shown = documents[document]
        found |= {node_key(document, node) for node in await find(session)}
        for child, element, loader in await child_frames(page, session):
            key = document_key(node_key(document, element), loader)
            documents[key] = (child, shown and await visible(session, element))
            pending.append(key)
        if len(documents) > FRAME_LIMIT:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
    return found, documents


async def shown_keys(documents: Documents, keys: set[int] | frozenset[int],
                     visible: Callable[[Session, int], Awaitable[bool]]) -> frozenset[int]:
    """The keys whose node shows, in a document still in the page whose frame elements all show."""
    shown_now = set()
    for key in keys:
        document, node = split_key(key)
        # A key from a document its frame has since navigated away from names no document here.
        session, shown = documents.get(document, (None, False))
        if session is not None and shown and await visible(session, node):
            shown_now.add(key)
    return frozenset(shown_now)


async def child_frames(client: Attaching, session: Session) -> list[tuple[FrameSession, int, object]]:
    """Each out-of-process frame directly inside `session`'s document: its session, its frame element's
    backendNodeId and the loader id of the document it shows.

    Chrome reports every existing child frame before answering the auto-attach. A frame that appears later is
    found by the next probe; one whose owner or loader can't be read fails the page check.
    """
    start = len(client.attached)
    await session.call("Target.setAutoAttach", autoAttach=True, waitForDebuggerOnStart=False, flatten=True)
    frames = []
    for target in client.attached[start:]:
        info = target.get("targetInfo") or {}
        if target.get("parentSessionId") != session.session_id or info.get("type") != "iframe":
            continue
        child = target.get("sessionId")
        if not isinstance(child, str) or not child:
            raise BrowserSessionError(PAGE_CHECK_FAILED)
        owner = (await session.call("DOM.getFrameOwner", frameId=info.get("targetId"))).get("backendNodeId")
        frame = FrameSession(client, child)
        tree = await frame.call("Page.getFrameTree")
        loader = ((tree.get("frameTree") or {}).get("frame") or {}).get("loaderId")
        frames.append((frame, node_key(None, owner), loader))
    return frames
