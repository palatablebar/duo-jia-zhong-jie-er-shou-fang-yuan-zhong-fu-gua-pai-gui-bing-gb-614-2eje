from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CONFIRM_ROLES
from src.controllers import bank_list_controller, property_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.correction import ConfirmIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/properties", tags=["properties"])

SORT_KEYS = {"risk", "spread", "members", "recent"}


@router.get("")
def list_properties(
    q: str | None = None,
    risk_level: str | None = None,
    tag: str | None = None,
    multi: bool | None = None,
    agency_id: int | None = None,
    sort: str = Query("risk"),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return property_controller.list_properties(
        db,
        q=q,
        risk_level=risk_level,
        tag=tag,
        multi=multi,
        agency_id=agency_id,
        sort=sort if sort in SORT_KEYS else "risk",
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/{property_id}")
def get_property(
    property_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return property_controller.get_property(db, property_id)


@router.get("/{property_id}/evidence")
def get_property_evidence(
    property_id: int, db: Session = Depends(get_db), principal: dict = Depends(require_roles(*READ_ROLES))
) -> dict:
    return property_controller.get_evidence(db, property_id)


@router.post("/{property_id}/confirm", status_code=201)
def confirm_property(
    property_id: int,
    payload: ConfirmIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CONFIRM_ROLES)),
) -> dict:
    return bank_list_controller.confirm(db, property_id, payload.model_dump(), principal, request)
