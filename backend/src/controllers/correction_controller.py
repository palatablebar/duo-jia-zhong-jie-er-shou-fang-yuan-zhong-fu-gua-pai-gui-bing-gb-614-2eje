"""挂牌修正控制器。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, correction_service


def apply(db: Session, payload: dict, principal: dict, request: Request) -> tuple[dict, int]:
    result = correction_service.apply_correction(db, payload, operator_email=principal["email"])
    correction = result["correction"]
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="correction.duplicate" if result["deduplicated"] else "correction.apply",
        entity="listing_correction",
        entity_id=correction["id"],
        detail={
            "correction_key": correction["correction_key"],
            "version": correction["version"],
            "changed": result["changed"],
            "affected": result["affected_property_ids"],
            "invalidated_evidence": correction["invalidated_evidence"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result, 200 if result["deduplicated"] else 201


def list_corrections(db: Session, **filters) -> dict:
    return correction_service.list_corrections(db, **filters)


def listing_corrections(db: Session, listing_id: int) -> dict:
    items = correction_service.listing_corrections(db, listing_id)
    return {"items": items, "total": len(items)}
