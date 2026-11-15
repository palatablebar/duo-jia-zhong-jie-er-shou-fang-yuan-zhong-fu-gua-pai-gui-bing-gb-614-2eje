"""主档确认：估价师确认某房源簇当前 revision 的 golden record 与证据。

修正导致 ``properties.revision`` 递增后，旧确认自动落后（不再代表当前值）；
受影响房源再次确认即「受影响后重新确认」，在银行清单上单独标出。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class PropertyConfirmation(Base):
    __tablename__ = "property_confirmations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_by: Mapped[str] = mapped_column(String(160), default="")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
