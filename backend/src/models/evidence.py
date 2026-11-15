"""主档字段证据：golden record 每个字段（面积/价格/户型）采信了哪家中介、哪条原文。

上游修正到达时，引用被修正挂牌版本的证据行置为 ``invalidated``（保留历史），
重算后为受影响房源簇写入新的 ``valid`` 证据行。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class FieldEvidence(Base):
    __tablename__ = "field_evidences"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(20), index=True)  # area / price / layout
    value: Mapped[str] = mapped_column(String(60), default="")
    rule: Mapped[str] = mapped_column(String(20), default="mode")  # mode / median
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"), index=True)
    agency_id: Mapped[int] = mapped_column(ForeignKey("agencies.id", ondelete="CASCADE"))
    # 证据取自哪一版挂牌：NULL = 原始采集版本（v0），否则指向修正版本
    correction_id: Mapped[int | None] = mapped_column(
        ForeignKey("listing_corrections.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_text: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="valid", index=True)
    invalidated_by: Mapped[int | None] = mapped_column(
        ForeignKey("listing_corrections.id", ondelete="SET NULL"), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("match_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
