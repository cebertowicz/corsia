"""Billing for an External endpoint: its mode and, for a subscription, its price and charge day."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

from connections.vendor_accounts import AccountError
from core.measurement_pricing import endpoint_listings
from infra.database import get_db
from infra.db_async import run_in_db

router = APIRouter(tags=["network-servers"])


class EndpointBillingWrite(BaseModel):
    # None unsets the endpoint: its cost then reads as not recorded, never as free.
    mode: str | None
    monthly_price_cents: StrictInt | None = None
    charge_day: StrictInt | None = None
    expected_billing_revision: StrictInt = Field(ge=0)


def billing_views(server_ids: list[str]) -> dict[str, dict]:
    """Each endpoint's ``billing`` and ``billing_revision``, plus whether its host lists prices."""
    from connections.subscription_billing import endpoint_billing

    db = get_db()
    try:
        views = endpoint_billing(db, server_ids)
        listed = endpoint_listings(db)
        return {server_id: view | {"list_priced": server_id in listed} for server_id, view in views.items()}
    finally:
        db.close()


@router.put("/{server_id}/billing")
async def put_billing(server_id: str, body: EndpointBillingWrite):
    """Record how this External endpoint is billed, effective from today."""
    from connections.subscription_billing import BillingFactValidationError, EndpointNotFound, set_endpoint_billing

    try:
        await run_in_db(
            set_endpoint_billing, server_id, mode=body.mode, monthly_price_cents=body.monthly_price_cents,
            charge_day=body.charge_day, expected_billing_revision=body.expected_billing_revision)
    except BillingFactValidationError as error:
        raise HTTPException(422, str(error)) from error
    except EndpointNotFound as error:
        raise HTTPException(404, detail=str(error)) from error
    except AccountError as error:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": error.code})
    return {"server_id": server_id, **(await run_in_db(billing_views, [server_id]))[server_id]}
