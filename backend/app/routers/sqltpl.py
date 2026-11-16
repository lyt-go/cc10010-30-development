"""SQL模板接口：模板登记、版本发布、版本文件下载/上传与版本包导入。

路由顺序有讲究：``/export``、``/import`` 这类静态路径必须排在 ``/{entry_id}``
之前，否则会被当成整型路径参数而返回 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.sqltpl import SqltplService, TemplateError
from app.template_repo import version_summary

router = APIRouter(prefix="/api/sqltpl", tags=["SQL模板"])

service = SqltplService()

LIST_FIELDS = ["模板编号", "模板名称", "模板类型", "SQL片段", "参数占位符", "预期输出列", "参数默认值", "模板版本"]
STATUSES = ["草稿", "模板评审中", "已发布", "已废弃"]


def _error(exc: TemplateError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "message": exc.message, "entry": exc.entry},
    )


def _download(file_name: str, payload: bytes, media_type: str) -> Response:
    # 文件名可能含中文，用 RFC 5987 的 filename* 交给浏览器处理。
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(file_name)}"
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按模板编号或名称检索"),
    status: str | None = Query(default=None, description="草稿、模板评审中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按模板编号与状态过滤SQL模板列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出SQL模板清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "sqltpl", "total": total, "items": items}


@router.get("/export/bundle")
def export_bundle() -> Response:
    """打包导出所有已发布/已替代版本，可直接通过 /import 再次导入。"""
    file_name, payload = service.export_bundle_bytes()
    return _download(file_name, payload, "application/json")


@router.post("/import")
async def import_bundle(request: Request) -> Any:
    """导入版本包：支持 multipart 文件上传或直接提交 JSON。

    版本包里的 SQL 片段逐字保留；内容已存在的版本复用，不重复生成。
    """
    content_type = request.headers.get("content-type", "")
    file_name = "inline.json"
    template_no: str | None = None
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="multipart 导入必须包含 file 字段")
        raw = await upload.read()  # type: ignore[union-attr]
        file_name = getattr(upload, "filename", None) or "bundle.json"
        template_no = str(form.get("模板编号") or "").strip() or None
    else:
        raw = await request.body()
    try:
        result = service.import_bundle(
            raw, file_name=file_name, template_no_override=template_no
        )
    except TemplateError as exc:
        return _error(exc)
    return {"ok": True, "message": "版本包已导入", **result}


@router.post("/{template_id}/versions", response_model=ActionResult)
def create_version(template_id: int, payload: EntryPayload) -> Any:
    """为模板创建草稿版本；参数占位符与默认值必须和 SQL 片段相互匹配。"""
    try:
        version, created = service.create_version(template_id, payload.values)
    except TemplateError as exc:
        return _error(exc)
    return ActionResult(
        ok=True,
        message="草稿版本已创建" if created else "已复用已有版本",
        entry={"version": version_summary(version)},
    )


@router.get("/{template_id}/versions", response_model=PageResult[dict])
def list_versions(
    template_id: int,
    status: str | None = Query(default=None, description="草稿、已发布、已替代"),
    page: int = 1,
    size: int = 20,
) -> Any:
    """列出模板的全部版本，按版本 ID 倒序。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    try:
        items, total = service.list_versions(template_id, status=status, page=page, size=size)
    except TemplateError as exc:
        return _error(exc)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/{template_id}/versions/upload")
async def upload_version(template_id: int, request: Request) -> Any:
    """上传新的 SQL 文件生成草稿版本；占位符可从当前发布版本继承。"""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="上传接口需要 multipart/form-data")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(status_code=400, detail="缺少 file 字段")
    raw = await upload.read()  # type: ignore[union-attr]
    file_name = getattr(upload, "filename", None) or "template.sql"
    try:
        result = service.upload_version_file(
            template_id,
            raw=raw,
            file_name=file_name,
            version_label=str(form.get("模板版本") or "").strip() or None,
            placeholders=str(form.get("参数占位符") or "").strip() or None,
            defaults=str(form.get("参数默认值") or "").strip() or None,
            expected_columns=str(form.get("预期输出列") or "").strip() or None,
        )
    except TemplateError as exc:
        return _error(exc)
    return {"ok": True, "file": file_name, **result}


@router.post("/{template_id}/publish-latest", response_model=ActionResult)
def publish_latest(template_id: int, payload: EntryPayload | None = None) -> Any:
    """发布该模板最新的草稿版本；并发发布时只有一个版本会成为生效版本。"""
    expected = None
    if payload is not None:
        expected = str(payload.values.get("快照摘要") or "").strip() or None
    try:
        result = service.publish_latest_draft(template_id, expected_digest=expected)
    except TemplateError as exc:
        return _error(exc)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/{template_id}/versions/{version_id}", response_model=dict)
def version_detail(template_id: int, version_id: int) -> Any:
    """读取版本文档，含完整不可变快照。"""
    try:
        return service.version_detail(template_id, version_id)
    except TemplateError as exc:
        return _error(exc)


@router.get("/{template_id}/versions/{version_id}/file")
def download_version_file(template_id: int, version_id: int) -> Any:
    """下载版本对应的 SQL 文件。"""
    try:
        file_name, payload = service.version_file(template_id, version_id)
    except TemplateError as exc:
        return _error(exc)
    return _download(file_name, payload, "application/sql; charset=utf-8")


@router.get("/{template_id}/versions/{version_id}/impact", response_model=dict)
def version_impact(template_id: int, version_id: int) -> Any:
    """发布前查看影响面：参数契约差异与仍绑定旧版本的作业。"""
    from app.services.etljob import EtljobService

    try:
        return EtljobService().version_impact(template_id, version_id)
    except Exception as exc:  # JobBindingError 与 TemplateError 都带 status_code
        status_code = getattr(exc, "status_code", 400)
        message = getattr(exc, "message", str(exc))
        return JSONResponse(
            status_code=status_code, content={"ok": False, "message": message}
        )


@router.post("/{template_id}/versions/{version_id}/publish", response_model=ActionResult)
def publish_version(template_id: int, version_id: int, payload: EntryPayload | None = None) -> Any:
    """发布指定版本；同一模板同时只保留一个已发布版本。"""
    expected = None
    if payload is not None:
        expected = str(payload.values.get("快照摘要") or "").strip() or None
    try:
        result = service.publish_version(template_id, version_id, expected_digest=expected)
    except TemplateError as exc:
        return _error(exc)
    return ActionResult(ok=True, message=result["message"], entry=result)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> Any:
    """读取单条模板定义明细（含全部版本摘要）；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"模板定义 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条模板定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="模板定义已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条模板定义执行提交模板评审、发布模板、废弃模板；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
