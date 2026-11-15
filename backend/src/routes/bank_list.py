from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.controllers import bank_list_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.utils.pagination import page_params

router = APIRouter(prefix="/bank-list", tags=["bank-list"])


@router.get("")
def list_bank_list(
    q: str | None = None,
    confirmation: str | None = None,
    require_confirmed: bool = False,
    limit: int = Query(50),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return bank_list_controller.list_bank_list(
        db,
        q=q,
        confirmation=confirmation,
        require_confirmed=require_confirmed,
        limit=page.limit,
        offset=page.offset,
    )
