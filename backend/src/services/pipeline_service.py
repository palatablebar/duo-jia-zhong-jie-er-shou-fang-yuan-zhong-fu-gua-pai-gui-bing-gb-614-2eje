"""归并流水线：归一（②）→ 实体解析（③④）→ 风险识别（⑤）→ 落库 → 字段证据。

纯后端计算，确定性；同一批原始挂牌两次执行得到完全相同的簇集合与 golden record。
落库是「按需写入」：只有内容真正变化的房源簇才会更新（updated_at 才移动），
未受影响的簇保持原值、证据与确认不变；每次运行后为新建/变化/被修正波及的簇
重建字段证据（旧证据行作废留痕，不物理删除）。
"""
from __future__ import annotations

import hashlib
import time

from sqlalchemy import func, select

from src.constants.limits import SIMILARITY_THRESHOLD
from src.models.agency import Agency
from src.models.confirmation import PropertyConfirmation
from src.models.evidence import FieldEvidence
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.models.review import Review
from src.services import entity_resolution, evidence_service, normalizer, risk_rules
from src.utils.timeutil import iso_utc

LISTING_FIELDS = (
    "community_raw",
    "layout_raw",
    "area_raw",
    "price_raw",
    "floor_raw",
    "orientation_raw",
    "title",
)

# golden record + 风险读数：任一变化都算「主档内容变化」
PROPERTY_VALUE_FIELDS = (
    "community",
    "layout",
    "area_mode",
    "area_min",
    "area_max",
    "price_median",
    "price_min",
    "price_max",
    "price_p10",
    "price_p90",
    "spread",
    "dispersion",
    "agency_count",
    "member_count",
    "confidence",
    "risk_score",
    "risk_level",
    "risk_tags",
    "risk_detail",
)


def cluster_key_of(agency_code: str, lid: str) -> str:
    return f"{agency_code}:{lid}"


def _apply_normalized(listing: Listing, norm: dict) -> None:
    """归一结果写回挂牌；值未变不赋值，避免无意义 UPDATE。"""
    for key, value in norm.items():
        if getattr(listing, key) != value:
            setattr(listing, key, value)


def _assign_if_changed(prop: Property, values: dict) -> bool:
    changed = False
    for key, value in values.items():
        if getattr(prop, key) != value:
            setattr(prop, key, value)
            changed = True
    return changed


def run_pipeline(
    db,
    run: MatchRun,
    threshold: float = SIMILARITY_THRESHOLD,
    *,
    force_evidence_ids: set[int] | None = None,
    invalidated_by: int | None = None,
) -> dict:
    started = time.perf_counter()
    listings = list(db.scalars(select(Listing).order_by(Listing.id)).all())
    agencies = {agency.id: agency for agency in db.scalars(select(Agency)).all()}
    if not listings:
        return {
            "metrics": {"listings": 0, "clusters": 0, "multi_clusters": 0, "pairs_scored": 0, "pairs_linked": 0},
            "log": "没有可归并的挂牌：请先采集入库。",
            "status": "partial",
        }

    raw_rows = [{field: getattr(listing, field) for field in LISTING_FIELDS} for listing in listings]
    normalized = [normalizer.normalize_listing(raw) for raw in raw_rows]
    for listing, norm in zip(listings, normalized, strict=True):
        _apply_normalized(listing, norm)

    agency_ids = [listing.agency_id for listing in listings]
    result = entity_resolution.resolve(normalized, agency_ids, threshold=threshold)

    existing = {prop.cluster_key: prop for prop in db.scalars(select(Property)).all()}
    seen_keys: set[str] = set()
    changed_ids: set[int] = set()
    members_by_property: dict[int, list[Listing]] = {}
    baits = stales = risk_high = risk_medium = 0

    for cluster in result["clusters"]:
        members = cluster["members"]
        member_lids = sorted(
            cluster_key_of(agencies[listings[index].agency_id].code, listings[index].lid) for index in members
        )
        key = hashlib.sha256("|".join(member_lids).encode("utf-8")).hexdigest()[:40]
        seen_keys.add(key)
        golden = cluster["golden"]
        member_rows = [
            {
                "id": listings[index].id,
                "lid": listings[index].lid,
                "price_num": normalized[index]["price_num"],
                "listed_days": listings[index].listed_days,
                "views": listings[index].views,
                "status": listings[index].status,
            }
            for index in members
        ]
        risk = risk_rules.scan_cluster(member_rows, golden)
        values = {
            "community": golden["community"],
            "layout": golden["layout"],
            "area_mode": golden["area_mode"],
            "area_min": golden["area_min"],
            "area_max": golden["area_max"],
            "price_median": golden["price_median"],
            "price_min": golden["price_min"],
            "price_max": golden["price_max"],
            "price_p10": golden["price_p10"],
            "price_p90": golden["price_p90"],
            "spread": golden["spread"],
            "dispersion": golden["dispersion"],
            "agency_count": golden["agency_count"],
            "member_count": golden["member_count"],
            "confidence": cluster["confidence"],
            "risk_score": risk["risk_score"],
            "risk_level": risk["risk_level"],
            "risk_tags": risk["risk_tags"],
            "risk_detail": risk["risk_detail"],
        }

        prop = existing.get(key)
        if prop is None:
            prop = Property(cluster_key=key, run_id=run.id, is_current=True)
            _assign_if_changed(prop, values)
            db.add(prop)
            db.flush()
            changed_ids.add(prop.id)
        else:
            changed = _assign_if_changed(prop, values)
            if not prop.is_current:
                prop.is_current = True
                changed = True
            if changed:
                prop.run_id = run.id
                changed_ids.add(prop.id)
        members_by_property[prop.id] = [listings[index] for index in members]

        bait_lids = set(risk["risk_detail"]["bait_lids"])
        stale_lids = set(risk["risk_detail"]["stale_lids"])
        for index in members:
            listing = listings[index]
            tags: list[str] = []
            if listing.lid in bait_lids:
                tags.append("bait")
            if listing.lid in stale_lids:
                tags.append("stale")
            if listing.property_id != prop.id:
                listing.property_id = prop.id
            if (listing.risk_tags or []) != tags:
                listing.risk_tags = tags
        baits += len(bait_lids)
        stales += len(stale_lids)
        if prop.risk_level == "high":
            risk_high += 1
        elif prop.risk_level == "medium":
            risk_medium += 1

    pruned = _prune_orphans(db, seen_keys)

    # 字段证据：新建/内容变化/被修正波及的簇重建；顺带补上没有任何有效证据的当前簇
    rebuild_ids = set(changed_ids) | {pid for pid in (force_evidence_ids or set()) if pid}
    valid_counts = dict(
        db.execute(
            select(FieldEvidence.property_id, func.count())
            .where(FieldEvidence.status == "valid")
            .group_by(FieldEvidence.property_id)
        ).all()
    )
    for prop in db.scalars(select(Property).where(Property.is_current.is_(True))).all():
        if not valid_counts.get(prop.id):
            rebuild_ids.add(prop.id)
    evidence_stats = evidence_service.sync_properties(
        db,
        rebuild_ids,
        run_id=run.id,
        invalidated_by=invalidated_by,
        members_by_property=members_by_property,
    )
    evidence_service.refresh_completeness(db)

    metrics = dict(result["metrics"])
    metrics.update(
        {
            "baits": baits,
            "stales": stales,
            "risk_high": risk_high,
            "risk_medium": risk_medium,
            "risk_total": risk_high + risk_medium,
            "pruned_clusters": pruned,
            "changed_properties": len(changed_ids),
            "evidence_invalidated": evidence_stats["invalidated"],
            "evidence_created": evidence_stats["created"],
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    )
    log_lines = [
        f"输入挂牌 {metrics['listings']} 条，面积归一失败 {sum(1 for n in normalized if n['area_num'] is None)} 条",
        f"blocking 候选对 {metrics['pairs_scored']} 组，阈值 {threshold:.2f} 连边 {metrics['pairs_linked']} 条",
        f"并查集 + 传递闭包得到 {metrics['clusters']} 个簇，其中多成员簇 {metrics['multi_clusters']} 个，去重率 {metrics['dedup_rate']:.2%}",
        f"算法 B：钓鱼价 {baits} 条、僵尸挂牌 {stales} 条，高风险簇 {risk_high} 个、中风险簇 {risk_medium} 个",
        f"淘汰旧簇 {pruned} 个；主档变化 {len(changed_ids)} 个；证据作废 {evidence_stats['invalidated']} 条、新落 {evidence_stats['created']} 条；耗时 {metrics['duration_ms']} ms",
    ]
    return {
        "metrics": metrics,
        "log": "\n".join(log_lines),
        "status": "success",
        "changed_property_ids": sorted(changed_ids),
    }


def _prune_orphans(db, seen_keys: set[str]) -> int:
    """删掉本次重算不再产生、且没有复核单的旧簇（有复核单的保留并标记为非当前）。"""
    db.flush()  # 先把本轮挂牌→新簇的归属落库，避免按旧归属误解除关联
    removed = 0
    for prop in db.scalars(select(Property)).all():
        if prop.cluster_key in seen_keys:
            continue
        reviews = db.scalar(select(func.count()).select_from(Review).where(Review.property_id == prop.id)) or 0
        if reviews:
            if prop.is_current:
                prop.is_current = False
            continue
        for listing in db.scalars(select(Listing).where(Listing.property_id == prop.id)).all():
            listing.property_id = None
        for evidence in db.scalars(select(FieldEvidence).where(FieldEvidence.property_id == prop.id)).all():
            db.delete(evidence)
        for confirmation in db.scalars(
            select(PropertyConfirmation).where(PropertyConfirmation.property_id == prop.id)
        ).all():
            db.delete(confirmation)
        db.delete(prop)
        removed += 1
    db.flush()
    return removed


def last_run_stamp(db) -> str | None:
    run = db.scalar(select(MatchRun).order_by(MatchRun.id.desc()).limit(1))
    return iso_utc(run.finished_at) if run else None
