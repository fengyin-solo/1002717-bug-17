"""闸口管理接口：围绕「识别 → 称重 → 放行」作业流，提供异常兜底动作与统一总览口径。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.gate import DIRECTIONS, GateService

router = APIRouter(prefix="/api/gate", tags=["闸口管理"])

service = GateService()

LIST_FIELDS = ["闸口编号", "闸口类型", "车道编号", "进出方向_展示", "OCR识别", "地磅称重", "放行抬杆", "闸口状态"]
STAGE_FILTERS = [
    "待识别", "识别受阻", "待称重", "称重受阻",
    "待放行", "抬杆失败", "信息待补", "已放行",
]


@router.get("/summary")
def summary() -> dict[str, Any]:
    """闸口总览：列表页与运营概览页共用这一份口径（在场车数随抬杆记录重算）。"""
    return service.summary()


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按闸口编号/车道编号/车牌检索"),
    direction: str | None = Query(default=None, description="进出方向：进场、出场；留空查全部"),
    stage: str | None = Query(default=None, description="车道阶段：识别受阻、称重受阻、抬杆失败、已放行等"),
    abnormal: bool | None = Query(default=None, description="只看异常（缺字段或任一环节受阻/失败）"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按进出方向与阶段过滤闸口记录；条件留空时返回全部，不把列表过滤空。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if direction and direction not in DIRECTIONS:
        raise HTTPException(status_code=400, detail="进出方向只支持：进场、出场；留空即查全部")
    items, total = service.list_entries(
        keyword=keyword, direction=direction or None, stage=stage or None,
        abnormal=abnormal, page=page, size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出闸口清单：返回全量抬杆记录（含总览口径）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "gate", "total": total, "items": items, "summary": service.summary()}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条抬杆记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"抬杆记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一辆到闸车辆；闸口/类型/车道必填，进出方向允许稍后补录。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="抬杆记录已登记", entry=entry)


@router.post("/{entry_id}/ocr", response_model=ActionResult)
def recognize(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """OCR 重识别：认不出时先返回原因，成功再放行下一步；带车牌即按现场补录。"""
    values = payload.values if payload is not None else {}
    entry, message, reason = service.recognize(entry_id, values.get("车牌"))
    if entry is None:
        return ActionResult(ok=False, message=message, detail=reason)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/weigh", response_model=ActionResult)
def weigh(entry_id: int) -> ActionResult:
    """地磅取重：取不到时后端自动重新取一次接着走，不让前端干等。"""
    entry, message, reason, recovered = service.weigh(entry_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/release", response_model=ActionResult)
def release(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """自动放行：同车牌同方向只放一次；抬杆失败恢复未放行并保留重量。"""
    values = payload.values if payload is not None else {}
    entry, message, missing = service.lift(
        entry_id, manual=False,
        plate=values.get("车牌"), direction=values.get("进出方向"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/manual-lift", response_model=ActionResult)
def manual_lift(entry_id: int, payload: EntryPayload | None = None) -> ActionResult:
    """人工抬杆：与自动放行冲突时以现场确认为准；同一车牌仍只放行一次。"""
    values = payload.values if payload is not None else {}
    entry, message, missing = service.lift(
        entry_id, manual=True,
        plate=values.get("车牌"), direction=values.get("进出方向"),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/supplement", response_model=ActionResult)
def supplement(entry_id: int, payload: EntryPayload) -> ActionResult:
    """补录进出方向/车牌：缺关键信息时先补信息再放行。"""
    entry, message = service.supplement(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
