from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CORRECTION_ROLES
from src.controllers import correction_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.correction import CorrectionIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/corrections", tags=["corrections"])


@router.get("")
def list_corrections(
    agency_code: str | None = None,
    lid: str | None = None,
    listing_id: int | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return correction_controller.list_corrections(
        db, agency_code=agency_code, lid=lid, listing_id=listing_id, limit=page.limit, offset=page.offset
    )


@router.post("")
def apply_correction(
    payload: CorrectionIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CORRECTION_ROLES)),
) -> dict:
    result, status_code = correction_controller.apply(db, payload.model_dump(), principal, request)
    response.status_code = status_code
    return result
