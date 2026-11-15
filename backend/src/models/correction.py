"""挂牌修正版本：上游中介每次修正挂牌都追加一条，永不覆盖旧版本。

自然键 ``correction_key``（上游修正单号）：同一修正重试时直接命中已有记录，
不新增版本、不重复触发证据失效与重算（幂等）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ListingCorrection(Base):
    __tablename__ = "listing_corrections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    correction_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 变更明细：[{field, old, new}, ...]；noop=True 表示修正内容与现值一致（只留痕不重算）
    changes: Mapped[list] = mapped_column(JSON, default=list)
    noop: Mapped[bool] = mapped_column(default=False)
    source: Mapped[str] = mapped_column(String(20), default="agency_feed")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    affected_property_ids: Mapped[list] = mapped_column(JSON, default=list)
    invalidated_evidence: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
