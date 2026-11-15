"""银行清单：只列当前有效且证据齐全的房源簇，并标出受影响后重新确认的结果。

- 入选门槛：``is_current``（最近一次重算仍产出该簇）且 ``evidence_complete``
  （面积/价格/户型都至少有一条有效证据）；
- 每行带字段证据摘要（采信了哪家中介、哪条原文）与确认状态；
- ``reconfirmed=True`` 表示该房源曾被上游修正波及（revision > 0），
  且最新确认是在受影响之后作出的（受影响后重新确认）。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import CONFIRMATION_STATE_LABELS, EVIDENCE_FIELD_LABELS
from src.models.agency import Agency
from src.models.confirmation import PropertyConfirmation
from src.models.evidence import FieldEvidence
from src.models.listing import Listing
from src.models.property import Property
from src.services import evidence_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, utcnow


def _evidence_digest(db: Session, property_ids: list[int]) -> dict[int, dict]:
    """每个房源簇三字段的有效证据摘要：{pid: {field: [{agency_name, lid, source_text, rule}]}}"""
    if not property_ids:
        return {}
    rows = db.scalars(
        select(FieldEvidence)
        .where(FieldEvidence.property_id.in_(property_ids), FieldEvidence.status == "valid")
        .order_by(FieldEvidence.id)
    ).all()
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    listings = {listing.id: listing for listing in db.scalars(select(Listing)).all()}
    digest: dict[int, dict] = {}
    for row in rows:
        bucket = digest.setdefault(row.property_id, {})
        bucket.setdefault(row.field, []).append(
            {
                "agency_name": agencies[row.agency_id].name if row.agency_id in agencies else "",
                "lid": listings[row.listing_id].lid if row.listing_id in listings else None,
                "source_text": row.source_text,
                "rule": row.rule,
                "value": row.value,
            }
        )
    return digest


def _latest_confirmations(db: Session, property_ids: list[int]) -> dict[int, PropertyConfirmation]:
    if not property_ids:
        return {}
    rows = db.scalars(
        select(PropertyConfirmation)
        .where(PropertyConfirmation.property_id.in_(property_ids))
        .order_by(PropertyConfirmation.id)
    ).all()
    latest: dict[int, PropertyConfirmation] = {}
    for row in rows:
        latest[row.property_id] = row
    return latest


def _serialize_item(prop: Property, digest: dict, confirmation: PropertyConfirmation | None, last_affected: str | None) -> dict:
    state = evidence_service.confirmation_state(prop, confirmation)
    evidence = {}
    for field in evidence_service.TRACKED_FIELDS:
        supporters = digest.get(field, [])
        evidence[field] = {
            "field_label": EVIDENCE_FIELD_LABELS.get(field, field),
            "value": supporters[0]["value"] if supporters else "",
            "complete": bool(supporters),
            "supporters": supporters,
        }
    return {
        "property_id": prop.id,
        "community": prop.community,
        "layout": prop.layout,
        "area_mode": prop.area_mode,
        "price_median": prop.price_median,
        "agency_count": prop.agency_count,
        "member_count": prop.member_count,
        "risk_level": prop.risk_level,
        "revision": prop.revision,
        "evidence_complete": prop.evidence_complete,
        "evidence": evidence,
        "confirmation_state": state,
        "confirmation_state_label": CONFIRMATION_STATE_LABELS.get(state, state),
        "reconfirmed": state == "reconfirmed",
        "confirmed_by": confirmation.confirmed_by if confirmation and confirmation.revision >= prop.revision else None,
        "confirmed_at": iso_utc(confirmation.created_at)
        if confirmation and confirmation.revision >= prop.revision
        else None,
        "last_affected_at": last_affected,
        "updated_at": iso_utc(prop.updated_at),
    }


def bank_list(
    db: Session,
    *,
    q: str | None = None,
    confirmation: str | None = None,
    require_confirmed: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    stmt = select(Property).where(Property.is_current.is_(True), Property.evidence_complete.is_(True))
    count_stmt = select(func.count()).select_from(Property).where(
        Property.is_current.is_(True), Property.evidence_complete.is_(True)
    )
    if q:
        like = f"%{q.strip()}%"
        condition = or_(Property.community.like(like), Property.layout.like(like))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    total = db.scalar(count_stmt) or 0
    props = list(db.scalars(stmt.order_by(Property.id).limit(limit).offset(offset)).all())
    ids = [prop.id for prop in props]
    digests = _evidence_digest(db, ids)
    confirmations = _latest_confirmations(db, ids)
    affected = {pid: evidence_service.last_affected_at(db, pid) for pid in ids}
    items = []
    for prop in props:
        item = _serialize_item(prop, digests.get(prop.id, {}), confirmations.get(prop.id), affected[prop.id])
        items.append(item)
    if confirmation:
        items = [item for item in items if item["confirmation_state"] == confirmation]
    if require_confirmed:
        items = [item for item in items if item["confirmation_state"] in {"confirmed", "reconfirmed"}]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def confirm_property(db: Session, property_id: int, *, confirmed_by: str, note: str | None) -> dict:
    """确认当前 revision 的主档与证据；受影响过的房源再次确认即「重新确认」。"""
    prop = db.get(Property, property_id)
    if prop is None:
        raise not_found("property", property_id)
    if not prop.is_current:
        raise AppError("CONFLICT", 409, {"property_id": property_id, "reason": "stale_cluster"})
    if not prop.evidence_complete:
        raise AppError("CONFLICT", 409, {"property_id": property_id, "reason": "evidence_incomplete"})
    confirmation = PropertyConfirmation(
        property_id=prop.id,
        revision=prop.revision,
        confirmed_by=confirmed_by,
        note=note,
        created_at=utcnow(),
    )
    db.add(confirmation)
    db.flush()
    digest = _evidence_digest(db, [prop.id]).get(prop.id, {})
    return _serialize_item(prop, digest, confirmation, evidence_service.last_affected_at(db, prop.id))


def bank_list_rows(db: Session) -> list[dict]:
    """导出用：全量符合条件的清单行（不分页）。"""
    props = list(
        db.scalars(
            select(Property)
            .where(Property.is_current.is_(True), Property.evidence_complete.is_(True))
            .order_by(Property.id)
        ).all()
    )
    ids = [prop.id for prop in props]
    digests = _evidence_digest(db, ids)
    confirmations = _latest_confirmations(db, ids)
    affected = {pid: evidence_service.last_affected_at(db, pid) for pid in ids}
    return [
        _serialize_item(prop, digests.get(prop.id, {}), confirmations.get(prop.id), affected[prop.id])
        for prop in props
    ]
