"""银行清单控制器。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, bank_list_service


def list_bank_list(db: Session, **filters) -> dict:
    return bank_list_service.bank_list(db, **filters)


def confirm(db: Session, property_id: int, payload: dict, principal: dict, request: Request) -> dict:
    item = bank_list_service.confirm_property(
        db,
        property_id,
        confirmed_by=principal["email"],
        note=payload.get("note"),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="property.confirm",
        entity="property",
        entity_id=property_id,
        detail={
            "revision": item["revision"],
            "confirmation_state": item["confirmation_state"],
            "reconfirmed": item["reconfirmed"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return item
