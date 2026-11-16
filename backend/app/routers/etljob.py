"""ETL作业接口：作业定义维护与模板版本绑定。

静态路径（``/export``、``/bindings``）必须排在 ``/{entry_id}`` 之前，
否则会被当作整型路径参数解析而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.etljob import EtljobService, JobBindingError

router = APIRouter(prefix="/api/etljob", tags=["ETL作业"])

service = EtljobService()

LIST_FIELDS = ["作业编号", "作业名称", "源端数据表", "目标数据表", "执行SQL", "调度表达式", "超时分钟", "失败重试次数"]
STATUSES = ["草稿", "作业评审中", "已发布", "已下线"]


def _error(exc: JobBindingError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "message": exc.message, "entry": exc.entry},
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按作业编号或名称检索"),
    status: str | None = Query(default=None, description="草稿、作业评审中、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按作业编号与状态过滤ETL作业列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出ETL作业清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "etljob", "total": total, "items": items}


@router.get("/bindings", response_model=PageResult[dict])
def list_bindings(
    job_id: int | None = Query(default=None, description="只看某个作业的绑定"),
    template_no: str | None = Query(default=None, description="按模板编号过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """查询作业与模板版本的绑定结果，可按作业或模板过滤。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_bindings(job_id=job_id, template_no=template_no, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/templates/{template_id}/versions", response_model=PageResult[dict])
def available_versions(template_id: int, page: int = 1, size: int = 50) -> Any:
    """作业侧可绑定的版本列表：只含已发布与已替代（历史）版本。"""
    try:
        items = service.list_available_versions(template_id)
    except JobBindingError as exc:
        return _error(exc)
    start = max(page - 1, 0) * size
    return PageResult(
        items=items[start:start + size],
        total=len(items),
        page=page,
        size=size,
    )


@router.post("/{entry_id}/bind-template", response_model=ActionResult)
def bind_template(entry_id: int, payload: EntryPayload) -> Any:
    """把作业绑定到不可变模板版本，校验参数并生成渲染 SQL。

    请求可带 ``模板ID``/``模板编号``、``模板版本ID``/``模板版本``、``参数绑定``
    与 ``对侧作业编号``；同版本同参数重复提交只返回原绑定。
    """
    try:
        result = service.bind_template_version(entry_id, payload.values)
    except JobBindingError as exc:
        return _error(exc)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/{entry_id}/binding", response_model=dict)
def get_binding(entry_id: int) -> Any:
    """回查作业当前绑定的模板版本、快照摘要、参数绑定与渲染 SQL。"""
    try:
        return service.job_binding(entry_id)
    except JobBindingError as exc:
        return _error(exc)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> Any:
    """读取单条作业定义明细（含当前绑定与绑定历史）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"作业定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条作业定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="作业定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条作业定义执行提交作业评审、发布作业、下线作业；未绑定模板版本时不允许发布。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
