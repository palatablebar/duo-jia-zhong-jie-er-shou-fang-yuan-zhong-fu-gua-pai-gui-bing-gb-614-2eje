"""挂牌修正入参：上游中介修正挂牌（幂等键 correction_key）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# 允许修正的挂牌字段：原文字段（参与归一）+ 状态/天数/带看（参与风险识别）
TEXT_FIELDS = {
    "community_raw": 160,
    "layout_raw": 60,
    "area_raw": 60,
    "price_raw": 60,
    "floor_raw": 60,
    "orientation_raw": 40,
    "title": 200,
}
INT_FIELDS = {"listed_days", "views"}
STATUS_VALUES = {"active", "sold", "off_market"}


class CorrectionIn(BaseModel):
    correction_key: str = Field(min_length=4, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    agency_code: str = Field(min_length=1, max_length=40)
    lid: str = Field(min_length=1, max_length=40)
    changes: dict[str, Any]
    source: Literal["agency_feed", "manual", "api"] = "agency_feed"
    note: str | None = Field(default=None, max_length=500)

    @field_validator("changes")
    @classmethod
    def _check_changes(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("changes 至少要有一个字段")
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in TEXT_FIELDS:
                if not isinstance(item, str):
                    raise ValueError(f"{key} 必须是字符串")
                text = item.strip()
                if not text:
                    raise ValueError(f"{key} 不能为空")
                if len(text) > TEXT_FIELDS[key]:
                    raise ValueError(f"{key} 超长（>{TEXT_FIELDS[key]} 字）")
                cleaned[key] = text
            elif key in INT_FIELDS:
                if isinstance(item, bool) or not isinstance(item, int):
                    raise ValueError(f"{key} 必须是非负整数")
                if item < 0 or item > 100000:
                    raise ValueError(f"{key} 超出合理范围")
                cleaned[key] = item
            elif key == "status":
                if item not in STATUS_VALUES:
                    raise ValueError(f"status 只能是 {'/'.join(sorted(STATUS_VALUES))}")
                cleaned[key] = item
            else:
                raise ValueError(f"不支持修正字段 {key}")
        return cleaned


class ConfirmIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
