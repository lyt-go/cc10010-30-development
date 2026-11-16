"""模板版本与作业绑定的数据访问层。

这里集中所有对 ``sqltpl_version`` 与 ``etljob_binding`` 两张内存表的读写，
并用 ``store.lock`` 把「发布版本」「绑定作业」这类需要原子性的操作串起来。
版本一旦发布就只读：任何作业绑定拿到的都是同一份快照摘要。
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from app.store import store
from app.template_core import canonical_json, content_signature, parameter_signature, sha256_text

TEMPLATE_MODULE = "sqltpl"
VERSION_MODULE = "sqltpl_version"
BINDING_MODULE = "etljob_binding"

VERSION_DRAFT = "草稿"
VERSION_PUBLISHED = "已发布"
VERSION_SUPERSEDED = "已替代"


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def find_template_by_no(template_no: str) -> dict[str, Any] | None:
    for row in store.rows(TEMPLATE_MODULE):
        if str(row.get("模板编号")) == str(template_no):
            return row
    return None


def versions_of(template_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(VERSION_MODULE) if int(row.get("模板ID", 0)) == template_id]


def find_version(template_id: int, version_id: int) -> dict[str, Any] | None:
    for row in versions_of(template_id):
        if int(row.get("id", 0)) == version_id:
            return row
    return None


def find_version_by_id(version_id: int) -> dict[str, Any] | None:
    return store.find(VERSION_MODULE, version_id)


def find_version_by_label(template_id: int, label: str) -> dict[str, Any] | None:
    for row in versions_of(template_id):
        if str(row.get("模板版本")) == str(label):
            return row
    return None


def active_version(template_id: int) -> dict[str, Any] | None:
    for row in versions_of(template_id):
        if row.get("status") == VERSION_PUBLISHED:
            return row
    return None


def version_snapshot(version: dict[str, Any]) -> dict[str, Any]:
    """返回快照的深拷贝，调用方无法通过返回值改到已发布版本。"""
    return copy.deepcopy(version.get("快照") or {})


def is_bindable(version: dict[str, Any] | None) -> bool:
    """草稿不能绑定；已发布与已替代（历史版本）都可以被作业继续使用。"""
    return bool(version) and version.get("status") in (VERSION_PUBLISHED, VERSION_SUPERSEDED)


def version_summary(version: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": version.get("id"),
        "模板ID": version.get("模板ID"),
        "模板编号": version.get("模板编号"),
        "模板版本": version.get("模板版本"),
        "修订号": version.get("修订号"),
        "status": version.get("status"),
        "快照摘要": version.get("快照摘要"),
        "参数签名": version.get("参数签名"),
        "内容签名": version.get("内容签名"),
        "参数占位符": copy.deepcopy(version.get("参数占位符") or []),
        "参数默认值": copy.deepcopy(version.get("参数默认值") or {}),
        "预期输出列": copy.deepcopy(version.get("预期输出列") or []),
        "SQL片段": version.get("SQL片段"),
        "来源": version.get("来源"),
        "创建时间": version.get("创建时间"),
        "发布时间": version.get("发布时间"),
        "创建人": version.get("创建人"),
        "基线版本ID": version.get("基线版本ID"),
        "参数冲突": copy.deepcopy(version.get("参数冲突") or []),
        "导入来源": version.get("导入来源"),
        "pending": version.get("pending", False),
        "abnormal": version.get("abnormal", False),
    }


def build_version_row(
    *,
    version_id: int,
    template_id: int,
    core: dict[str, Any],
    source: str,
    actor: str,
    status: str = VERSION_DRAFT,
    published_at: str | None = None,
    base_version_id: int | None = None,
    import_source: str | None = None,
) -> dict[str, Any]:
    """纯函数构造版本行：发布前后只有状态与时间不同，内容字段完全一致。"""
    digest = sha256_text(canonical_json(core))
    defaults = dict(core.get("参数默认值") or {})
    placeholders = list(core.get("参数占位符") or [])
    return {
        "id": version_id,
        "模板ID": int(template_id),
        "模板编号": core["模板编号"],
        "模板版本": core["模板版本"],
        "修订号": core["修订号"],
        "status": status,
        "pending": status == VERSION_DRAFT,
        "abnormal": False,
        "快照": copy.deepcopy(core),
        "快照摘要": digest,
        "参数签名": parameter_signature(placeholders, defaults),
        "内容签名": content_signature(
            template_no=core["模板编号"],
            sql_fragment=core["SQL片段"],
            placeholders=placeholders,
            defaults=defaults,
            expected_columns=list(core.get("预期输出列") or []),
        ),
        "参数占位符": placeholders,
        "参数默认值": defaults,
        "预期输出列": list(core.get("预期输出列") or []),
        "SQL片段": core["SQL片段"],
        "来源": source,
        "创建时间": now_iso(),
        "发布时间": published_at,
        "创建人": actor,
        "基线版本ID": base_version_id,
        "参数冲突": [],
        "导入来源": import_source,
    }


def make_version(
    *,
    template: dict[str, Any],
    core: dict[str, Any],
    source: str,
    actor: str,
    base_version_id: int | None = None,
    import_source: str | None = None,
) -> dict[str, Any]:
    """构造一条草稿版本；快照此时已经完整，后续发布只改状态不改内容。"""
    rows = store.rows(VERSION_MODULE)
    row = build_version_row(
        version_id=next_id(rows),
        template_id=int(template["id"]),
        core=core,
        source=source,
        actor=actor,
        base_version_id=base_version_id,
        import_source=import_source,
    )
    rows.append(row)
    return row


def freeze_version(version: dict[str, Any]) -> dict[str, Any]:
    """发布时冻结快照并复核摘要，防止草稿在发布前被别处改动过。"""
    core = copy.deepcopy(version.get("快照") or {})
    digest = sha256_text(canonical_json(core))
    stored = str(version.get("快照摘要") or "")
    if stored and stored != digest:
        raise ValueError("版本快照摘要不一致，拒绝发布")
    version["快照"] = core
    version["快照摘要"] = digest
    version["status"] = VERSION_PUBLISHED
    version["pending"] = False
    version["发布时间"] = now_iso()
    return version


def touch_template_publish_fields(template: dict[str, Any], version: dict[str, Any]) -> None:
    published = [row for row in versions_of(int(template["id"])) if row.get("status") in (VERSION_PUBLISHED, VERSION_SUPERSEDED)]
    template["当前发布版本"] = version.get("模板版本")
    template["当前发布版本ID"] = version.get("id")
    template["当前发布快照摘要"] = version.get("快照摘要")
    template["已发布版本数"] = len(published)
    template["最近发布时间"] = version.get("发布时间")
    if template.get("status") not in ("已发布", "已废弃"):
        template["status"] = "已发布"
    template["pending"] = False


def binding_fingerprint(job_id: int, version: dict[str, Any], parameters: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            {
                "作业ID": int(job_id),
                "版本ID": int(version["id"]),
                "快照摘要": version.get("快照摘要"),
                "参数绑定": {key: parameters[key] for key in sorted(parameters)},
            }
        )
    )


def find_binding_by_fingerprint(job_id: int, fingerprint: str) -> dict[str, Any] | None:
    for row in store.rows(BINDING_MODULE):
        if int(row.get("作业ID", 0)) == job_id and row.get("请求指纹") == fingerprint:
            return row
    return None


def bindings_of_job(job_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(BINDING_MODULE) if int(row.get("作业ID", 0)) == job_id]


def current_binding(job_id: int) -> dict[str, Any] | None:
    rows = bindings_of_job(job_id)
    return rows[-1] if rows else None


def make_binding(
    *,
    job: dict[str, Any],
    version: dict[str, Any],
    parameters: dict[str, Any],
    rendered_sql: str,
    fingerprint: str,
    counterpart_job_no: str | None = None,
) -> dict[str, Any]:
    rows = store.rows(BINDING_MODULE)
    binding_id = next_id(rows)
    row = {
        "id": binding_id,
        "作业ID": int(job["id"]),
        "作业编号": job.get("作业编号"),
        "模板ID": int(version["模板ID"]),
        "模板编号": version.get("模板编号"),
        "版本ID": int(version["id"]),
        "模板版本": version.get("模板版本"),
        "快照摘要": version.get("快照摘要"),
        "快照": version_snapshot(version),
        "参数绑定": dict(parameters),
        "参数绑定摘要": sha256_text(canonical_json({key: parameters[key] for key in sorted(parameters)})),
        "渲染SQL": rendered_sql,
        "请求指纹": fingerprint,
        "绑定状态": "已绑定",
        "绑定时间": now_iso(),
        "对侧作业编号": counterpart_job_no,
        "对侧绑定ID": None,
        "状态": "已生效",
    }
    rows.append(row)
    return row


def write_job_binding_fields(
    job: dict[str, Any],
    *,
    binding: dict[str, Any],
    template: dict[str, Any],
    counterpart_job_no: str | None,
    version_is_latest: bool,
) -> None:
    """把模板处理结果回写到 ETL 作业清单，列表页直接可查。"""
    job["模板编号"] = binding.get("模板编号")
    job["模板版本"] = binding.get("模板版本")
    job["模板版本ID"] = binding.get("版本ID")
    job["模板快照摘要"] = binding.get("快照摘要")
    job["参数绑定"] = copy.deepcopy(binding.get("参数绑定") or {})
    job["参数绑定摘要"] = binding.get("参数绑定摘要")
    job["渲染SQL"] = binding.get("渲染SQL")
    job["绑定ID"] = binding.get("id")
    job["绑定状态"] = binding.get("绑定状态")
    job["绑定时间"] = binding.get("绑定时间")
    job["模板发布版本"] = template.get("当前发布版本")
    job["模板发布状态"] = "已是最新" if version_is_latest else "发现新版本"
    job["模板最新发布版本"] = template.get("当前发布版本")
    job["最新快照摘要"] = template.get("当前发布快照摘要")
    job["对侧作业编号"] = counterpart_job_no or job.get("对侧作业编号") or ""
    job["对侧绑定版本"] = binding.get("模板版本") if counterpart_job_no else ""
    job["对侧版本一致"] = bool(counterpart_job_no)
    job["pending"] = job.get("pending", False)


def binding_detail(binding: dict[str, Any], template: dict[str, Any] | None = None) -> dict[str, Any]:
    version = find_version_by_id(int(binding.get("版本ID", 0)))
    active = active_version(int(binding.get("模板ID", 0))) if binding.get("模板ID") else None
    payload = copy.deepcopy(binding)
    payload["版本是否最新"] = bool(active and int(active["id"]) == int(binding.get("版本ID", 0)))
    payload["模板当前发布版本"] = (template or {}).get("当前发布版本") or (active or {}).get("模板版本")
    payload["模板当前发布版本ID"] = (template or {}).get("当前发布版本ID") or (active or {}).get("id")
    payload["模板状态"] = (template or {}).get("status")
    payload["版本状态"] = (version or {}).get("status")
    payload["参数默认值"] = copy.deepcopy((version or {}).get("参数默认值") or {})
    payload["参数占位符"] = copy.deepcopy((version or {}).get("参数占位符") or [])
    return payload
