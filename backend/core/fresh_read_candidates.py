"""Which models may read an item, in what order, and how much input a read may carry.

Order: every ready model cheaper than the writer's own, cheapest first (the local seat
costs nothing in money and comes first), then the writer's own model, always last. A
model with no list price is not counted as cheaper, except the local seat. Locality
policy applies: a model the writer's scope may not send to is not resolved as a reader.

Input cap: one number per install, the smallest over the candidates of
``window - instructions - output allowance``, and no more than
``long-context threshold - instructions`` where the price file has one. The window and
output limit are the catalog's, the threshold the price file's; a call refused for
length counts as that candidate unavailable, so a wrong stored value costs a candidate,
never a delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from infra.database import get_db

#: Order key only, never a cost estimate shown to anyone: the design's typical update
#: (about 3,000 input and 300 output tokens, shared/substrate/DRAFT-fresh-reader-before-person.md).
_TYPICAL_READ_TOKENS = (3_000, 300)


@dataclass(frozen=True)
class Candidate:
    model: str
    window: int | None
    output_allowance: int
    long_context_bound: int | None
    server_id: str | None
    is_local: bool
    writer_own: bool


def _list_price(row: dict) -> float | None:
    """Dollars for a typical read at list price; the local seat is 0, an unpriced model None."""
    if row.get("is_local"):
        return 0.0
    from llm.api_equivalent import api_equivalent

    prompt, completion = _TYPICAL_READ_TOKENS
    return api_equivalent(str(row["litellm_model"]), prompt, completion, 0, 0)


def _output_allowance(model: str) -> int:
    """The model's own output limit from the catalog, capped as every call's output is."""
    from llm.capabilities import _DEFAULT_MAX_OUTPUT_TOKENS_CAP, get_model_max_output_tokens

    return get_model_max_output_tokens(model, _DEFAULT_MAX_OUTPUT_TOKENS_CAP)


def _candidate(row: dict | None, model: str, writer_own: bool) -> Candidate:
    from llm.api_equivalent import long_context_bound
    from llm.locality import row_server_id

    row = row or {}
    window = row.get("context_window")
    return Candidate(
        model=model, window=int(window) if isinstance(window, int) and window > 0 else None,
        output_allowance=_output_allowance(model), long_context_bound=long_context_bound(model),
        server_id=row_server_id(row) if row else None, is_local=bool(row.get("is_local")), writer_own=writer_own,
    )


def writer_model(writer_thread_id: str | None) -> str | None:
    if not writer_thread_id:
        return None
    db = get_db()
    try:
        row = db.execute("SELECT model FROM threads WHERE id=?", (writer_thread_id,)).fetchone()
    finally:
        db.close()
    return (str(row["model"] or "").strip() or None) if row else None


def candidates(writer_thread_id: str | None) -> list[Candidate]:
    """Blocking. The readers to try, in order; the writer's own model (when it has one) is last."""
    from llm.account_catalog import effective_status
    from llm.model_status import scoped_status_rows

    own = writer_model(writer_thread_id)
    rows = scoped_status_rows(thread_id=writer_thread_id)
    by_model = {str(row.get("litellm_model")): row for row in rows if row.get("litellm_model")}
    own_price = _list_price(by_model[own]) if own in by_model else None
    cheaper: list[tuple[float, str, dict]] = []
    for model, row in by_model.items():
        if model == own or effective_status(row)[0] != "ready":
            continue
        price = _list_price(row)
        if price is None or (own_price is not None and price >= own_price):
            continue
        cheaper.append((price, model, row))
    ordered = [_candidate(row, model, False) for _price, model, row in sorted(cheaper, key=lambda c: (c[0], c[1]))]
    if own is not None:
        ordered.append(_candidate(by_model.get(own), own, True))
    return ordered


@lru_cache(maxsize=64)
def instruction_tokens(model: str) -> int:
    """The reader's own instructions, measured from the prompt text once per model."""
    from core.fresh_read_prompt import SYSTEM_PROMPT
    from core.token_estimation import measure_text

    return measure_text(SYSTEM_PROMPT, model=model).tokens


def candidate_cap(candidate: Candidate) -> int | None:
    """This candidate's room for the reader's input, or None when the catalog gives no window or threshold."""
    room = instruction_tokens(candidate.model)
    caps = []
    if candidate.window is not None:
        caps.append(candidate.window - room - candidate.output_allowance)
    if candidate.long_context_bound is not None:
        caps.append(candidate.long_context_bound - room)
    return max(min(caps), 0) if caps else None


def install_cap(found: list[Candidate]) -> int | None:
    """The smallest candidate cap, so the same item gets the same input whichever reader answers."""
    caps = [cap for cap in (candidate_cap(candidate) for candidate in found) if cap is not None]
    return min(caps) if caps else None


async def seat_waiting(candidate: Candidate) -> bool:
    """Whether the local seat reports any waiting request; an unreadable status reports none."""
    if not candidate.is_local or candidate.server_id is None:
        return False
    import httpx

    from infra.carry_in_capacity import STATUS_TIMEOUT_SECONDS, _status_address
    from infra.db_async import run_in_db
    from llm.providers.omlx_public_probe import probe_registered_status

    address = await run_in_db(_status_address, candidate.server_id)
    if address is None:
        return False
    async with httpx.AsyncClient(timeout=httpx.Timeout(STATUS_TIMEOUT_SECONDS)) as client:
        probed = await probe_registered_status(client, candidate.server_id, *address)
    waiting = ((probed[2] if probed else None) or {}).get("waiting_requests")
    return isinstance(waiting, int) and waiting > 0
