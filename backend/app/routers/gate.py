"""闸口管理接口：进出闸通行（OCR 识别、地磅取重、放行/人工抬杆、现场确认）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.gate import FLOW_STATUSES, service

router = APIRouter(prefix="/api/gate", tags=["闸口管理"])

LIST_FIELDS = ["闸口编号", "闸口类型", "车道编号", "进出方向", "车牌号码", "OCR识别", "地磅称重", "放行抬杆", "闸口状态"]


@router.get("/overview")
def gate_overview() -> dict[str, Any]:
    """闸口总览：在场车数等卡片随抬杆记录实时重算，和列表同一口径。"""
    return service.overview()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出闸口通行清单：返回全量数据（注意要排在 /{entry_id} 之前）。"""
    items = service.all_entries()
    return {"module": "gate", "total": len(items), "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按闸口编号/车道/车牌检索"),
    status: str | None = Query(default=None, description="通行状态，见闸口状态字典"),
    direction: str | None = Query(default=None, description="进场 / 出场"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列表：字段为空也照常返回该行，空值展示与缺项说明由服务层给出。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if status and status not in FLOW_STATUSES:
        raise HTTPException(status_code=400, detail=f"闸口状态仅支持：{'、'.join(FLOW_STATUSES)}")
    items, total = service.list_entries(keyword=keyword, status=status, direction=direction, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/statuses")
def gate_statuses() -> dict[str, Any]:
    """闸口状态字典：列表页与总览页共用，避免两处状态两样。"""
    return {"statuses": FLOW_STATUSES, "directions": ["进场", "出场"]}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条进出闸明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"进出闸 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一辆到闸车辆；缺必填字段时说明缺哪几项，不静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}", entry=None)
    return ActionResult(ok=True, message="进出闸已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行通行动作；设备抖动由服务内自动重试，失败时给出原因并保留可重试入口。"""
    action = str(payload.values.get("action") or "").strip()
    if not action:
        return ActionResult(ok=False, message="缺少动作名称（action）")
    entry, message, ok = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=ok, message=message, entry=entry)
