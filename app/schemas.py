"""HTTP 请求/响应模型（Pydantic；坐标数值校验在服务层做有限性检查）。"""
from __future__ import annotations

from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class CoordModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 允许任意数值（含 inf/nan 透传），有限性在服务层显式拒绝并给出明确报错
    x: float
    y: float


class WindowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = Field(..., description="生效开始 UTC ISO 8601（含）")
    end: str = Field(..., description="生效结束 UTC ISO 8601（不含）")


class ZoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone_id: str = Field(..., min_length=1)
    # 顶点为 [x, y]，首尾点无需重复
    exterior: List[List[float]] = Field(..., min_length=3)
    holes: List[List[List[float]]] = Field(default_factory=list)
    windows: List[WindowModel] = Field(..., min_length=1)


class TrajectoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="UTC ISO 8601，必须带时区")
    x: float
    y: float


class PointBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: List[TrajectoryPoint] = Field(..., min_length=1)


class RevokeItem(BaseModel):
    """撤销项：该 point_id 从车次有效点集中永久移除。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["revoke"]
    point_id: str = Field(..., min_length=1)


class UpdateItem(BaseModel):
    """更正项：将该 point_id 改写为新的 UTC 时刻与有限平面坐标。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["update"]
    point_id: str = Field(..., min_length=1)
    ts: str = Field(..., description="更正后的 UTC ISO 8601 时刻，必须带时区")
    x: float
    y: float


CorrectionItem = Annotated[Union[RevokeItem, UpdateItem], Field(discriminator="action")]


class CorrectionBatch(BaseModel):
    """现场采集点更正批：整组在同一事务内核对并生效。"""

    model_config = ConfigDict(extra="forbid")

    base_version: int = Field(
        ..., ge=0, description="作业人员所见的当前轨迹版本；须与库中当前版本一致"
    )
    reason: str = Field(..., min_length=1, description="非空更正原因")
    items: List[CorrectionItem] = Field(..., min_length=1)


class CoOccupancyQuery(BaseModel):
    """多车同时占用审计请求：指定危险区在 [since, until) 内被多车同时占用的时段。"""

    model_config = ConfigDict(extra="forbid")

    trip_ids: List[str] = Field(
        ..., min_length=2, description="至少两辆互异车次（重复车次在服务层拒绝）"
    )
    since: str = Field(..., description="查询起始 UTC ISO 8601（含），必须带时区")
    until: str = Field(..., description="查询结束 UTC ISO 8601（不含），必须带时区")
    threshold: int = Field(
        ..., ge=2, description="同时占用阈值：2..车次数（超出车次数在服务层拒绝）"
    )
    revision: Optional[int] = Field(
        default=None, ge=1, description="可选区域修订号：按截至该号的区域配置核算"
    )
    track_versions: Dict[str, Annotated[int, Field(ge=0)]] = Field(
        default_factory=dict,
        description="可选逐车轨迹版本 {trip_id: 版本号}；未列出的车次取查询时最新已提交状态",
    )
