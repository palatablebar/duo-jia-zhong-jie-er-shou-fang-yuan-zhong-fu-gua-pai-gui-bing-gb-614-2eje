"""字段证据服务：golden record 每个字段采信了哪家中介、哪条原文。

- 面积取簇内众数、价格取簇内中位、户型取簇内众数；每个字段把支撑该取值的
  成员挂牌逐条落为证据行（含中介、挂牌编号、修正版本、采集原文）。
- 上游修正到达：引用被修正挂牌的证据行置为 invalidated（保留历史），
  受影响房源簇重算后写入新的 valid 证据行。
- ``properties.evidence_complete``：面积/价格/户型都至少有一条有效证据。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CONFIRMATION_STATE_LABELS,
    EVIDENCE_FIELD_LABELS,
    EVIDENCE_RULE_LABELS,
    EVIDENCE_STATUS_LABELS,
    EvidenceField,
    EvidenceRule,
    EvidenceStatus,
)
from src.models.agency import Agency
from src.models.confirmation import PropertyConfirmation
from src.models.correction import ListingCorrection
from src.models.evidence import FieldEvidence
from src.models.listing import Listing
from src.models.property import Property
from src.utils.timeutil import iso_utc, utcnow

TRACKED_FIELDS: tuple[str, ...] = (
    EvidenceField.AREA.value,
    EvidenceField.PRICE.value,
    EvidenceField.LAYOUT.value,
)

_RULE_OF = {
    EvidenceField.AREA.value: EvidenceRule.MODE.value,
    EvidenceField.PRICE.value: EvidenceRule.MEDIAN.value,
    EvidenceField.LAYOUT.value: EvidenceRule.MODE.value,
}

_SOURCE_TEXT_OF = {
    EvidenceField.AREA.value: lambda listing: listing.area_raw,
    EvidenceField.PRICE.value: lambda listing: listing.price_raw,
    EvidenceField.LAYOUT.value: lambda listing: listing.layout_raw,
}


def field_value(prop: Property, field: str) -> str:
    """主档字段当前取值的规范字符串（空值记空串，视为证据缺失）。"""
    if field == EvidenceField.AREA.value:
        return f"{prop.area_mode:g}" if prop.area_mode else ""
    if field == EvidenceField.PRICE.value:
        return str(int(prop.price_median)) if prop.price_median else ""
    if field == EvidenceField.LAYOUT.value:
        return prop.layout or ""
    return ""


def supporters(prop: Property, listings: list[Listing]) -> dict[str, list[Listing]]:
    """每个字段实际采信的成员挂牌（证据来源）。"""
    area_sup: list[Listing] = []
    if prop.area_mode:
        target = round(float(prop.area_mode), 1)
        area_sup = [
            listing
            for listing in listings
            if listing.area_num is not None and abs(round(float(listing.area_num), 1) - target) < 1e-9
        ]

    price_sup: list[Listing] = []
    priced = sorted(
        (listing for listing in listings if listing.price_num),
        key=lambda listing: (int(listing.price_num), listing.id),
    )
    if prop.price_median and priced:
        exact = [listing for listing in priced if int(listing.price_num) == int(prop.price_median)]
        if exact:
            price_sup = exact
        else:
            # 偶数条时中位是中间两条的均值：两条都是证据来源
            middle = len(priced) // 2
            picks = {priced[middle].id} if len(priced) % 2 == 1 else {priced[middle - 1].id, priced[middle].id}
            price_sup = [listing for listing in priced if listing.id in picks]

    layout_sup: list[Listing] = []
    if prop.layout:
        layout_sup = [listing for listing in listings if (listing.layout_norm or "") == prop.layout]

    return {
        EvidenceField.AREA.value: area_sup,
        EvidenceField.PRICE.value: price_sup,
        EvidenceField.LAYOUT.value: layout_sup,
    }


def build_rows(prop: Property, listings: list[Listing], run_id: int | None) -> list[FieldEvidence]:
    """按当前 golden record 生成有效证据行（未入库）。"""
    sup = supporters(prop, listings)
    rows: list[FieldEvidence] = []
    for field in TRACKED_FIELDS:
        value = field_value(prop, field)
        if not value:
            continue
        for listing in sup[field]:
            rows.append(
                FieldEvidence(
                    property_id=prop.id,
                    field=field,
                    value=value,
                    rule=_RULE_OF[field],
                    listing_id=listing.id,
                    agency_id=listing.agency_id,
                    correction_id=listing.current_correction_id,
                    source_text=_SOURCE_TEXT_OF[field](listing) or "",
                    status=EvidenceStatus.VALID.value,
                    run_id=run_id,
                )
            )
    return rows


def _members_by_property(db: Session, property_ids: set[int]) -> dict[int, list[Listing]]:
    grouped: dict[int, list[Listing]] = {pid: [] for pid in property_ids}
    if not property_ids:
        return grouped
    rows = db.scalars(
        select(Listing).where(Listing.property_id.in_(property_ids)).order_by(Listing.id)
    ).all()
    for listing in rows:
        grouped.setdefault(listing.property_id, []).append(listing)
    return grouped


def sync_properties(
    db: Session,
    property_ids: set[int],
    *,
    run_id: int | None = None,
    invalidated_by: int | None = None,
    members_by_property: dict[int, list[Listing]] | None = None,
) -> dict:
    """重建指定房源簇的证据：作废旧 valid 行，按当前主档写入新行。"""
    stats = {"rebuilt": 0, "invalidated": 0, "created": 0}
    ids = {pid for pid in property_ids if pid}
    if not ids:
        return stats
    props = {prop.id: prop for prop in db.scalars(select(Property).where(Property.id.in_(ids))).all()}
    if members_by_property is None:
        members_by_property = _members_by_property(db, set(props))
    now = utcnow()
    for pid, prop in props.items():
        if not prop.is_current:
            continue  # 过期簇的证据保留为历史，不再重建
        old_rows = db.scalars(
            select(FieldEvidence).where(
                FieldEvidence.property_id == pid,
                FieldEvidence.status == EvidenceStatus.VALID.value,
            )
        ).all()
        for row in old_rows:
            row.status = EvidenceStatus.INVALIDATED.value
            row.invalidated_at = now
            row.invalidated_by = invalidated_by
            stats["invalidated"] += 1
        members = members_by_property.get(pid) or []
        new_rows = build_rows(prop, members, run_id)
        for row in new_rows:
            db.add(row)
        sup = supporters(prop, members)
        prop.evidence_complete = all(field_value(prop, field) and sup[field] for field in TRACKED_FIELDS)
        stats["rebuilt"] += 1
        stats["created"] += len(new_rows)
    db.flush()
    return stats


def invalidate_for_listing(db: Session, listing_id: int, correction: ListingCorrection) -> tuple[int, set[int]]:
    """上游修正到达：引用该挂牌的有效证据全部失效，返回受影响房源簇 id。"""
    rows = db.scalars(
        select(FieldEvidence).where(
            FieldEvidence.listing_id == listing_id,
            FieldEvidence.status == EvidenceStatus.VALID.value,
        )
    ).all()
    affected: set[int] = set()
    now = utcnow()
    for row in rows:
        row.status = EvidenceStatus.INVALIDATED.value
        row.invalidated_at = now
        row.invalidated_by = correction.id
        affected.add(row.property_id)
    db.flush()
    return len(rows), affected


def refresh_completeness(db: Session, property_ids: set[int] | None = None) -> int:
    """按有效证据行重算 evidence_complete（只写真正变化的行），返回修正数。"""
    stmt = select(Property)
    if property_ids is not None:
        if not property_ids:
            return 0
        stmt = stmt.where(Property.id.in_(property_ids))
    props = list(db.scalars(stmt).all())
    if not props:
        return 0
    ids = [prop.id for prop in props]
    rows = db.execute(
        select(FieldEvidence.property_id, FieldEvidence.field).where(
            FieldEvidence.property_id.in_(ids),
            FieldEvidence.status == EvidenceStatus.VALID.value,
        )
    ).all()
    fields_by_property: dict[int, set[str]] = {}
    for pid, field in rows:
        fields_by_property.setdefault(pid, set()).add(field)
    changed = 0
    for prop in props:
        complete = all(field in fields_by_property.get(prop.id, set()) for field in TRACKED_FIELDS)
        if prop.evidence_complete != complete:
            prop.evidence_complete = complete
            changed += 1
    db.flush()
    return changed


def ensure_coverage(db: Session, run_id: int | None = None) -> int:
    """给还没有任何有效证据的当前房源簇补建证据（seed / 老库迁移用，幂等）。"""
    current = list(db.scalars(select(Property).where(Property.is_current.is_(True))).all())
    if not current:
        return 0
    counts = dict(
        db.execute(
            select(FieldEvidence.property_id, func.count())
            .where(FieldEvidence.status == EvidenceStatus.VALID.value)
            .group_by(FieldEvidence.property_id)
        ).all()
    )
    missing = {prop.id for prop in current if not counts.get(prop.id)}
    if missing:
        sync_properties(db, missing, run_id=run_id)
    refresh_completeness(db)
    return len(missing)


def serialize(row: FieldEvidence, agency: Agency | None = None, listing: Listing | None = None) -> dict:
    return {
        "id": row.id,
        "property_id": row.property_id,
        "field": row.field,
        "field_label": EVIDENCE_FIELD_LABELS.get(row.field, row.field),
        "value": row.value,
        "rule": row.rule,
        "rule_label": EVIDENCE_RULE_LABELS.get(row.rule, row.rule),
        "listing_id": row.listing_id,
        "lid": listing.lid if listing else None,
        "agency_id": row.agency_id,
        "agency_name": agency.name if agency else "",
        "correction_id": row.correction_id,
        "source_text": row.source_text,
        "status": row.status,
        "status_label": EVIDENCE_STATUS_LABELS.get(row.status, row.status),
        "invalidated_by": row.invalidated_by,
        "invalidated_at": iso_utc(row.invalidated_at),
        "run_id": row.run_id,
        "created_at": iso_utc(row.created_at),
    }


def property_evidence(db: Session, property_id: int) -> dict:
    """某房源簇的字段证据：当前有效 + 已失效历史，按字段分组。"""
    rows = list(
        db.scalars(
            select(FieldEvidence)
            .where(FieldEvidence.property_id == property_id)
            .order_by(FieldEvidence.id)
        ).all()
    )
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    listings = {listing.id: listing for listing in db.scalars(select(Listing)).all()}
    grouped: dict[str, dict] = {}
    for field in TRACKED_FIELDS:
        current = [row for row in rows if row.field == field and row.status == EvidenceStatus.VALID.value]
        history = [row for row in rows if row.field == field and row.status != EvidenceStatus.VALID.value]
        grouped[field] = {
            "field": field,
            "field_label": EVIDENCE_FIELD_LABELS.get(field, field),
            "value": current[0].value if current else "",
            "complete": bool(current),
            "current": [serialize(row, agencies.get(row.agency_id), listings.get(row.listing_id)) for row in current],
            "history": [serialize(row, agencies.get(row.agency_id), listings.get(row.listing_id)) for row in history],
        }
    return grouped


def latest_confirmation(db: Session, property_id: int) -> PropertyConfirmation | None:
    return db.scalar(
        select(PropertyConfirmation)
        .where(PropertyConfirmation.property_id == property_id)
        .order_by(PropertyConfirmation.id.desc())
        .limit(1)
    )


def confirmation_state(prop: Property, confirmation: PropertyConfirmation | None) -> str:
    """确认状态：pending（未确认或已落后）/ confirmed / reconfirmed（受影响后重新确认）。"""
    if confirmation is None or confirmation.revision < prop.revision:
        return "pending"
    return "reconfirmed" if prop.revision > 0 else "confirmed"


def confirmation_state_label(state: str) -> str:
    return CONFIRMATION_STATE_LABELS.get(state, state)


def last_affected_at(db: Session, property_id: int) -> str | None:
    row = db.scalar(
        select(FieldEvidence.invalidated_at)
        .where(
            FieldEvidence.property_id == property_id,
            FieldEvidence.invalidated_at.is_not(None),
        )
        .order_by(FieldEvidence.invalidated_at.desc())
        .limit(1)
    )
    return iso_utc(row) if row else None
