"""归并簇控制器。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from src.services import property_service


def list_properties(db: Session, **filters) -> dict:
    return property_service.list_properties(db, **filters)


def get_property(db: Session, property_id: int) -> dict:
    return property_service.get_property(db, property_id)


def get_evidence(db: Session, property_id: int) -> dict:
    prop = property_service.get_property(db, property_id)
    return {
        "property_id": prop["id"],
        "community": prop["community"],
        "layout": prop["layout"],
        "evidence_complete": prop["evidence_complete"],
        "evidence": prop["evidence"],
        "confirmation": prop["confirmation"],
    }
