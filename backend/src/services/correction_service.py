"""挂牌修正服务：版本留存 → 证据失效 → 受影响房源簇重算（幂等）。

- 每条修正追加一个版本（``listing_corrections``），永不覆盖旧版本；
- 自然键 ``correction_key``：同一修正重试直接返回已有记录，不新增版本、
  不重复作废证据、不重复触发重算；
- 修正到达后：引用被修正挂牌的证据失效 → 全量确定性重算（按需写入）→
  受影响房源簇（证据失效或主档内容变化）revision +1 并重建证据；
- 未受影响的房源簇保持原值、证据与确认不变。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.limits import SIMILARITY_THRESHOLD
from src.models.agency import Agency
from src.models.correction import ListingCorrection
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.services import evidence_service, match_run_service, normalizer, pipeline_service
from src.utils.errors import not_found
from src.utils.timeutil import iso_utc, utcnow

# 会影响字段证据（面积/价格/户型）的修正字段
EVIDENCE_FIELDS = {"area_raw", "price_raw", "layout_raw"}


def serialize(correction: ListingCorrection, listing: Listing | None = None, agency: Agency | None = None) -> dict:
    return {
        "id": correction.id,
        "correction_key": correction.correction_key,
        "listing_id": correction.listing_id,
        "lid": listing.lid if listing else None,
        "agency_code": agency.code if agency else None,
        "agency_name": agency.name if agency else None,
        "version": correction.version,
        "changes": correction.changes or [],
        "noop": correction.noop,
        "source": correction.source,
        "note": correction.note,
        "operator_email": correction.operator_email,
        "run_id": correction.run_id,
        "affected_property_ids": correction.affected_property_ids or [],
        "invalidated_evidence": correction.invalidated_evidence,
        "created_at": iso_utc(correction.created_at),
    }


def _find_listing(db: Session, agency_code: str, lid: str) -> tuple[Agency, Listing]:
    agency = db.scalar(select(Agency).where(Agency.code == agency_code))
    if agency is None:
        raise not_found("agency", agency_code)
    listing = db.scalar(select(Listing).where(Listing.agency_id == agency.id, Listing.lid == lid))
    if listing is None:
        raise not_found("listing", f"{agency_code}:{lid}")
    return agency, listing


def _renormalize(listing: Listing) -> None:
    raw = {field: getattr(listing, field) for field in pipeline_service.LISTING_FIELDS}
    norm = normalizer.normalize_listing(raw)
    for key, value in norm.items():
        setattr(listing, key, value)


def apply_correction(db: Session, payload: dict, *, operator_email: str) -> dict:
    """应用一条上游修正；同一 correction_key 重试幂等去重。"""
    agency, listing = _find_listing(db, payload["agency_code"], payload["lid"])

    existing = db.scalar(
        select(ListingCorrection).where(ListingCorrection.correction_key == payload["correction_key"])
    )
    if existing is not None:
        return {
            "deduplicated": True,
            "changed": False,
            "correction": serialize(existing, listing, agency),
            "affected_property_ids": existing.affected_property_ids or [],
            "run": None,
            "message": "同一修正已应用过，本次重试未新增记录",
        }

    changes: dict = payload["changes"]
    diff = []
    for field, new_value in changes.items():
        old_value = getattr(listing, field)
        if old_value != new_value:
            diff.append({"field": field, "old": old_value, "new": new_value})

    version = (
        db.scalar(select(func.max(ListingCorrection.version)).where(ListingCorrection.listing_id == listing.id)) or 0
    ) + 1
    correction = ListingCorrection(
        correction_key=payload["correction_key"],
        listing_id=listing.id,
        version=version,
        changes=diff,
        noop=not diff,
        source=payload.get("source") or "agency_feed",
        note=payload.get("note"),
        operator_email=operator_email,
    )
    db.add(correction)
    db.flush()

    if not diff:
        # 修正内容与现值一致：留痕版本即可，不动证据、不重算
        correction.affected_property_ids = []
        db.flush()
        return {
            "deduplicated": False,
            "changed": False,
            "correction": serialize(correction, listing, agency),
            "affected_property_ids": [],
            "run": None,
            "message": "修正内容与当前值一致，已留痕，无需重算",
        }

    for item in diff:
        setattr(listing, item["field"], item["new"])
    _renormalize(listing)
    listing.current_correction_id = correction.id
    listing.updated_at = utcnow()
    db.flush()

    # ① 受影响证据失效（仅当修正触及面积/价格/户型原文）
    affected: set[int] = set()
    invalidated = 0
    if EVIDENCE_FIELDS & {item["field"] for item in diff}:
        invalidated, affected = evidence_service.invalidate_for_listing(db, listing.id, correction)

    # ② 重算相关房源（全量确定性重算 + 按需写入；未受影响簇保持不变）
    pre_existing_ids = set(db.scalars(select(Property.id)).all())
    run = MatchRun(
        run_no=match_run_service.next_run_no(db),
        trigger="correction",
        status="running",
        operator_email=operator_email,
        threshold=SIMILARITY_THRESHOLD,
    )
    db.add(run)
    db.flush()
    result = pipeline_service.run_pipeline(
        db,
        run,
        threshold=SIMILARITY_THRESHOLD,
        force_evidence_ids=affected,
        invalidated_by=correction.id,
    )
    metrics = result.get("metrics") or {}
    run.metrics = metrics
    run.log_text = result.get("log") or ""
    run.status = result.get("status") or "success"
    run.listing_count = int(metrics.get("listings") or 0)
    run.cluster_count = int(metrics.get("clusters") or 0)
    run.multi_count = int(metrics.get("multi_clusters") or 0)
    run.dedup_rate = float(metrics.get("dedup_rate") or 0.0)
    run.risk_count = int(metrics.get("risk_total") or 0)
    run.finished_at = utcnow()
    db.flush()

    # ③ 受影响房源簇 revision +1（证据失效 ∪ 主档内容变化），旧确认随之落后；
    #    本次重算才诞生的新簇不算「受影响」，revision 保持 0
    changed_ids = set(result.get("changed_property_ids") or [])
    affected |= changed_ids
    bumped = affected & pre_existing_ids
    if bumped:
        for prop in db.scalars(select(Property).where(Property.id.in_(bumped))).all():
            prop.revision = (prop.revision or 0) + 1
        db.flush()

    correction.run_id = run.id
    correction.affected_property_ids = sorted(affected)
    correction.invalidated_evidence = invalidated
    db.flush()

    return {
        "deduplicated": False,
        "changed": True,
        "correction": serialize(correction, listing, agency),
        "affected_property_ids": sorted(affected),
        "run": match_run_service.serialize(run),
        "message": f"修正已应用：{invalidated} 条证据失效，{len(affected)} 个房源簇重算",
    }


def list_corrections(
    db: Session,
    *,
    agency_code: str | None = None,
    lid: str | None = None,
    listing_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(ListingCorrection)
    count_stmt = select(func.count()).select_from(ListingCorrection)
    if listing_id:
        stmt = stmt.where(ListingCorrection.listing_id == listing_id)
        count_stmt = count_stmt.where(ListingCorrection.listing_id == listing_id)
    else:
        if agency_code:
            agency = db.scalar(select(Agency).where(Agency.code == agency_code))
            if agency is None:
                raise not_found("agency", agency_code)
            listing_ids = select(Listing.id).where(Listing.agency_id == agency.id)
            stmt = stmt.where(ListingCorrection.listing_id.in_(listing_ids))
            count_stmt = count_stmt.where(ListingCorrection.listing_id.in_(listing_ids))
        if lid:
            listing_ids = select(Listing.id).where(Listing.lid == lid)
            stmt = stmt.where(ListingCorrection.listing_id.in_(listing_ids))
            count_stmt = count_stmt.where(ListingCorrection.listing_id.in_(listing_ids))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(ListingCorrection.id.desc()).limit(limit).offset(offset)).all()
    listings = {row.id: row for row in db.scalars(select(Listing)).all()}
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    items = []
    for row in rows:
        listing = listings.get(row.listing_id)
        agency = agencies.get(listing.agency_id) if listing else None
        items.append(serialize(row, listing, agency))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def listing_corrections(db: Session, listing_id: int) -> list[dict]:
    listing = db.get(Listing, listing_id)
    if listing is None:
        raise not_found("listing", listing_id)
    agency = db.get(Agency, listing.agency_id)
    rows = db.scalars(
        select(ListingCorrection)
        .where(ListingCorrection.listing_id == listing_id)
        .order_by(ListingCorrection.version.desc())
    ).all()
    return [serialize(row, listing, agency) for row in rows]
