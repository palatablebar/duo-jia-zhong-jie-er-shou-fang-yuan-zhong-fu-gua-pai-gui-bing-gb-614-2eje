"""修正版本 / 字段证据 / 失效重算 / 幂等重试 / 银行清单 的业务验收。

覆盖题目口径：
- 保留每条挂牌的修正版本，主档每个字段记下实际证据（中介 + 原文）；
- 上游修正到达 → 受影响证据失效并重算相关房源；
- 同一修正重试不新增重复记录；已完成且未受影响的房源保持不变；
- 银行清单只列当前有效且证据齐全的房源，并标出受影响后重新确认的结果。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base
from src.models.agency import Agency
from src.models.correction import ListingCorrection
from src.models.evidence import FieldEvidence
from src.models.listing import Listing
from src.models.match_run import MatchRun
from src.models.property import Property
from src.schemas.correction import CorrectionIn
from src.services import bank_list_service, correction_service, evidence_service, match_run_service


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    with session_factory() as session:
        _seed_base(session)
        yield session


def _seed_base(db: Session) -> None:
    db.add_all(
        [
            Agency(code="agency_a", name="甲中介", kind="broker", enabled=True),
            Agency(code="agency_b", name="乙中介", kind="platform", enabled=True),
        ]
    )
    db.flush()
    agency_a = db.scalar(select(Agency).where(Agency.code == "agency_a"))
    agency_b = db.scalar(select(Agency).where(Agency.code == "agency_b"))

    def listing(agency_id: int, lid: str, **kwargs) -> Listing:
        base = dict(
            community_raw="翠湖名邸",
            layout_raw="3室2厅1卫",
            area_raw="89.5㎡",
            price_raw="598万",
            floor_raw="3/18",
            orientation_raw="南北通透",
            title="翠湖名邸 3室2厅 89.5平 南北通透 诚售",
            status="active",
            listed_days=30,
            views=3,
        )
        base.update(kwargs)
        return Listing(agency_id=agency_id, lid=lid, **base)

    db.add_all(
        [
            # 簇 1：三套平台/门店重复挂牌（价格 598/602/606 万）
            listing(agency_a.id, "A-1", price_raw="598万"),
            listing(agency_b.id, "B-1", price_raw="602万"),
            listing(agency_a.id, "A-2", price_raw="606万"),
            # 簇 2：单成员簇（对照组，不应被簇 1 的修正波及）
            listing(
                agency_b.id,
                "B-2",
                community_raw="滨江花园",
                layout_raw="2室1厅1卫",
                area_raw="70㎡",
                price_raw="400万",
                title="滨江花园 2室1厅 70平",
            ),
            # 簇 3：价格无法解析 → 证据不齐全，永远进不了银行清单
            listing(
                agency_a.id,
                "A-3",
                community_raw="长风一村",
                layout_raw="1室1厅1卫",
                area_raw="45㎡",
                price_raw="价格面议",
                title="长风一村 1室1厅 45平",
            ),
        ]
    )
    db.flush()
    match_run_service.create_and_execute(db, trigger="seed", operator_email="test@listingmerge.io", threshold=0.80)
    db.commit()


def _property_of(db: Session, lid: str) -> Property:
    listing = db.scalar(select(Listing).where(Listing.lid == lid))
    return db.get(Property, listing.property_id)


def _valid_evidence(db: Session, property_id: int) -> list[FieldEvidence]:
    return list(
        db.scalars(
            select(FieldEvidence).where(
                FieldEvidence.property_id == property_id, FieldEvidence.status == "valid"
            )
        ).all()
    )


def _correction(db: Session, key: str, lid: str, changes: dict, agency_code: str | None = None) -> dict:
    if agency_code is None:
        agency_code = db.scalar(select(Listing.agency_id).where(Listing.lid == lid))
        agency_code = db.get(Agency, agency_code).code
    payload = CorrectionIn(correction_key=key, agency_code=agency_code, lid=lid, changes=changes)
    return correction_service.apply_correction(db, payload.model_dump(), operator_email="analyst@listingmerge.io")


# ---------- 主档字段证据 ----------


def test_master_record_fields_carry_actual_evidence(db):
    prop = _property_of(db, "A-1")
    assert prop.member_count == 3 and prop.agency_count == 2
    evidence = evidence_service.property_evidence(db, prop.id)
    # 面积：三条都是 89.5㎡，众数证据齐（中介 + 原文可追）
    area_sources = {(row["agency_name"], row["source_text"]) for row in evidence["area"]["current"]}
    assert ("甲中介", "89.5㎡") in area_sources and ("乙中介", "89.5㎡") in area_sources
    # 价格：中位 602 万，采信乙中介那条原文
    assert evidence["price"]["value"] == "6020000"
    assert [row["source_text"] for row in evidence["price"]["current"]] == ["602万"]
    # 户型：众数 3-2-1
    assert evidence["layout"]["value"] == "3-2-1"
    assert evidence["layout"]["complete"] and evidence["area"]["complete"] and evidence["price"]["complete"]
    assert prop.evidence_complete is True


# ---------- 修正版本留存 + 证据失效 + 重算 ----------


def test_correction_keeps_version_invalidates_evidence_and_recomputes(db):
    prop = _property_of(db, "A-1")
    before_median = prop.price_median
    assert before_median == 6_020_000

    result = _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    assert result["changed"] is True and result["deduplicated"] is False

    # 版本留存
    correction = db.scalar(select(ListingCorrection).where(ListingCorrection.correction_key == "corr-0001"))
    assert correction.version == 1
    assert correction.changes == [{"field": "price_raw", "old": "598万", "new": "612万"}]
    listing = db.scalar(select(Listing).where(Listing.lid == "A-1"))
    assert listing.price_raw == "612万" and listing.price_num == 6_120_000
    assert listing.current_correction_id == correction.id

    # 受影响证据失效 + 主档重算
    db.refresh(prop)
    assert prop.price_median == 6_060_000  # 新中位 606 万
    assert prop.revision == 1
    invalidated = list(
        db.scalars(
            select(FieldEvidence).where(
                FieldEvidence.property_id == prop.id, FieldEvidence.status == "invalidated"
            )
        ).all()
    )
    assert invalidated, "旧证据应保留为已失效历史"
    assert all(row.invalidated_by == correction.id for row in invalidated)
    # 新证据指向修正后的版本与新的采信对象
    evidence = evidence_service.property_evidence(db, prop.id)
    assert evidence["price"]["value"] == "6060000"
    assert [row["lid"] for row in evidence["price"]["current"]] == ["A-2"]
    a1_rows = [row for row in evidence["area"]["current"] if row["lid"] == "A-1"]
    assert a1_rows and all(row["correction_id"] == correction.id for row in a1_rows)
    # 修正触发了一次 correction 重算运行
    run = db.get(MatchRun, correction.run_id)
    assert run.trigger == "correction" and run.status == "success"


def test_second_correction_appends_version_and_history_is_kept(db):
    _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    result = _correction(db, "corr-0002", "A-1", {"area_raw": "建面92"})
    assert result["changed"] is True
    versions = correction_service.listing_corrections(db, db.scalar(select(Listing.id).where(Listing.lid == "A-1")))
    assert [item["version"] for item in versions] == [2, 1]
    prop = _property_of(db, "A-1")
    assert prop.revision == 2
    # 面积众数仍是 89.5（2 票对 1 票），A-1 的面积证据不再有效
    evidence = evidence_service.property_evidence(db, prop.id)
    assert evidence["area"]["value"] == "89.5"
    assert {row["lid"] for row in evidence["area"]["current"]} == {"B-1", "A-2"}
    # 失效历史包含第一次修正的价格证据与本次的面积证据
    invalidated_fields = {row["field"] for row in evidence["area"]["history"] + evidence["price"]["history"]}
    assert {"area", "price"} <= invalidated_fields


# ---------- 幂等重试 ----------


def test_same_correction_retry_creates_no_duplicates(db):
    first = _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    db.commit()
    prop = _property_of(db, "A-1")
    stamp = prop.updated_at
    counts = (
        db.scalar(select(func.count()).select_from(ListingCorrection)),
        db.scalar(select(func.count()).select_from(FieldEvidence)),
        db.scalar(select(func.count()).select_from(MatchRun)),
    )
    retry = _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    assert retry["deduplicated"] is True and retry["changed"] is False
    assert retry["correction"]["id"] == first["correction"]["id"]
    assert (
        db.scalar(select(func.count()).select_from(ListingCorrection)),
        db.scalar(select(func.count()).select_from(FieldEvidence)),
        db.scalar(select(func.count()).select_from(MatchRun)),
    ) == counts
    db.refresh(prop)
    assert prop.updated_at == stamp and prop.revision == 1


def test_noop_correction_only_records_version(db):
    runs_before = db.scalar(select(func.count()).select_from(MatchRun))
    result = _correction(db, "corr-noop", "A-1", {"price_raw": "598万"})
    assert result["changed"] is False and result["correction"]["noop"] is True
    assert db.scalar(select(func.count()).select_from(MatchRun)) == runs_before
    prop = _property_of(db, "A-1")
    assert prop.revision == 0


# ---------- 未受影响房源保持不变 ----------


def test_unaffected_property_stays_untouched(db):
    other = _property_of(db, "B-2")
    other_stamp = other.updated_at
    other_evidence_ids = sorted(row.id for row in _valid_evidence(db, other.id))
    _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    db.refresh(other)
    assert other.updated_at == other_stamp
    assert other.revision == 0
    assert sorted(row.id for row in _valid_evidence(db, other.id)) == other_evidence_ids


def test_non_evidence_correction_does_not_invalidate(db):
    prop = _property_of(db, "B-2")
    result = _correction(db, "corr-views", "B-2", {"views": 9})
    assert result["changed"] is True
    assert result["correction"]["invalidated_evidence"] == 0
    db.refresh(prop)
    assert prop.revision == 0  # 主档与证据都没变
    assert all(row.status == "valid" for row in _valid_evidence(db, prop.id))


def test_correction_splitting_cluster_creates_fresh_properties(db):
    """修正把挂牌改出原簇：旧簇淘汰，新诞生的簇 revision 从 0 开始（不算受影响）。"""
    old_prop = _property_of(db, "A-1")
    old_id = old_prop.id
    result = _correction(db, "corr-community", "A-1", {"community_raw": "远洋天地"})
    assert result["changed"] is True
    assert db.get(Property, old_id) is None, "旧簇（无复核单）应被淘汰"
    new_prop = _property_of(db, "B-1")
    solo_prop = _property_of(db, "A-1")
    assert new_prop.id != old_id and solo_prop.id != old_id
    assert new_prop.member_count == 2 and solo_prop.member_count == 1
    assert new_prop.revision == 0 and solo_prop.revision == 0
    assert new_prop.evidence_complete and solo_prop.evidence_complete
    # 新簇进入银行清单，状态为待确认
    bank = bank_list_service.bank_list(db, limit=50)["items"]
    by_id = {item["property_id"]: item for item in bank}
    assert by_id[new_prop.id]["confirmation_state"] == "pending"
    assert by_id[solo_prop.id]["confirmation_state"] == "pending"


# ---------- 银行清单 ----------


def test_bank_list_only_valid_and_fully_evidenced(db):
    data = bank_list_service.bank_list(db, limit=50)
    ids = {item["property_id"] for item in data["items"]}
    prop_ok = _property_of(db, "A-1")
    prop_single = _property_of(db, "B-2")
    prop_dirty = _property_of(db, "A-3")
    assert prop_ok.id in ids and prop_single.id in ids
    assert prop_dirty.id not in ids, "价格无法解析 → 证据不齐全 → 不进银行清单"
    assert prop_dirty.evidence_complete is False
    for item in data["items"]:
        assert item["evidence_complete"] is True
        assert item["reconfirmed"] is False
        assert item["confirmation_state"] == "pending"


def test_bank_list_marks_reconfirmed_after_correction(db):
    prop = _property_of(db, "A-1")
    # 先确认 → 已确认
    item = bank_list_service.confirm_property(db, prop.id, confirmed_by="analyst@listingmerge.io", note="首评")
    assert item["confirmation_state"] == "confirmed" and item["reconfirmed"] is False
    # 上游修正波及 → 确认落后（待重新确认）
    _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    db.refresh(prop)
    state = bank_list_service.bank_list(db, q="翠湖名邸", limit=10)["items"][0]
    assert state["confirmation_state"] == "pending" and state["reconfirmed"] is False
    # 受影响后重新确认 → 银行清单标出
    again = bank_list_service.confirm_property(db, prop.id, confirmed_by="analyst@listingmerge.io", note="复评")
    assert again["confirmation_state"] == "reconfirmed" and again["reconfirmed"] is True
    listed = bank_list_service.bank_list(db, q="翠湖名邸", limit=10)["items"][0]
    assert listed["reconfirmed"] is True and listed["confirmed_by"] == "analyst@listingmerge.io"
    # 证据仍然齐全 → 留在清单内
    assert listed["evidence_complete"] is True


def test_confirm_rejected_when_evidence_incomplete(db):
    prop = _property_of(db, "A-3")
    from src.utils.errors import AppError

    with pytest.raises(AppError):
        bank_list_service.confirm_property(db, prop.id, confirmed_by="analyst@listingmerge.io", note=None)


# ---------- 入参校验 ----------


def test_correction_payload_validation():
    with pytest.raises(Exception):
        CorrectionIn(correction_key="bad key!", agency_code="a", lid="l", changes={"price_raw": "1万"})
    with pytest.raises(Exception):
        CorrectionIn(correction_key="k-0001", agency_code="a", lid="l", changes={})
    with pytest.raises(Exception):
        CorrectionIn(correction_key="k-0001", agency_code="a", lid="l", changes={"unknown_field": "x"})
    ok = CorrectionIn(correction_key="k-0001", agency_code="a", lid="l", changes={"price_raw": "598万", "views": 3})
    assert ok.changes["views"] == 3


def test_list_corrections_filters(db):
    _correction(db, "corr-0001", "A-1", {"price_raw": "612万"})
    _correction(db, "corr-0002", "B-2", {"views": 9}, agency_code="agency_b")
    all_items = correction_service.list_corrections(db, limit=10)
    assert all_items["total"] == 2
    by_agency = correction_service.list_corrections(db, agency_code="agency_b", limit=10)
    assert by_agency["total"] == 1 and by_agency["items"][0]["lid"] == "B-2"
    assert by_agency["items"][0]["agency_name"] == "乙中介"
    by_lid = correction_service.list_corrections(db, lid="A-1", limit=10)
    assert by_lid["total"] == 1 and by_lid["items"][0]["version"] == 1
