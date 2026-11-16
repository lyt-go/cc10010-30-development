"""启动时把示例模板补全成「有版本、有绑定」的状态。

``seed.SEED_ROWS`` 里 SQL模板 / ETL作业 仍然是占位文本，这里在服务启动时
把它们替换成可读的模板片段，并生成一版已发布版本与一个已完成绑定的作业，
这样打开页面就能看到「作业 → 模板版本 → 参数绑定」的完整链路。

这个模块只写内存数据，不参与业务校验；真正的发布与绑定校验在 services 里。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.template_core import build_core_snapshot, render_sql
from app.template_repo import (
    VERSION_SUPERSEDED,
    build_version_row,
    make_binding,
    binding_fingerprint,
    touch_template_publish_fields,
    write_job_binding_fields,
)

TEMPLATE_SAMPLES = [
    {
        "模板编号": "SQLT-0001",
        "模板名称": "订单增量汇总",
        "模板类型": "聚合",
        "SQL片段": (
            "SELECT dt, region_id, COUNT(*) AS order_cnt\n"
            "FROM ods_order\n"
            "WHERE dt = '{{ dt }}' AND region_id = {{ region_id }}\n"
            "GROUP BY dt, region_id"
        ),
        "参数占位符": ["dt", "region_id"],
        "参数默认值": {"dt": "2026-11-15", "region_id": 0},
        "预期输出列": ["dt", "region_id", "order_cnt"],
        "模板版本": "v1",
        "status": "已发布",
    },
    {
        "模板编号": "SQLT-0002",
        "模板名称": "支付明细清洗",
        "模板类型": "清洗",
        "SQL片段": (
            "SELECT order_id, pay_channel, pay_amount\n"
            "FROM ods_payment\n"
            "WHERE dt = '{{ dt }}' AND pay_channel IN ('{{ channel }}')"
        ),
        "参数占位符": ["dt", "channel"],
        "参数默认值": {"dt": "2026-11-15", "channel": "alipay"},
        "预期输出列": ["order_id", "pay_channel", "pay_amount"],
        "模板版本": "v1",
        "status": "模板评审中",
    },
    {
        "模板编号": "SQLT-0003",
        "模板名称": "退款宽表合并",
        "模板类型": "关联",
        "SQL片段": (
            "SELECT r.refund_id, o.order_id, r.refund_amount\n"
            "FROM ods_refund r\n"
            "JOIN dwd_order o ON r.order_id = o.order_id\n"
            "WHERE r.dt BETWEEN '{{ start_dt }}' AND '{{ end_dt }}'"
        ),
        "参数占位符": ["start_dt", "end_dt"],
        "参数默认值": {"start_dt": "2026-11-01", "end_dt": "2026-11-15"},
        "预期输出列": ["refund_id", "order_id", "refund_amount"],
        "模板版本": "v2",
        "status": "已发布",
    },
]


def _ensure_templates() -> None:
    templates = store.rows("sqltpl")
    for sample in TEMPLATE_SAMPLES:
        row = None
        for candidate in templates:
            if candidate.get("模板编号") == sample["模板编号"]:
                row = candidate
                break
        if row is None:
            continue
        row.update(
            {
                "模板名称": sample["模板名称"],
                "模板类型": sample["模板类型"],
                "SQL片段": sample["SQL片段"],
                "参数占位符": sample["参数占位符"],
                "参数默认值": sample["参数默认值"],
                "预期输出列": sample["预期输出列"],
                "模板版本": sample["模板版本"],
                "status": sample["status"],
                "pending": sample["status"] != "已发布",
            }
        )


def _publish_seed_version(template: dict[str, Any], sample: dict[str, Any], *, superseded: bool = False) -> dict[str, Any]:
    core = build_core_snapshot(
        template_no=sample["模板编号"],
        template_name=sample["模板名称"],
        template_type=sample["模板类型"],
        sql_fragment=sample["SQL片段"],
        placeholders=list(sample["参数占位符"]),
        defaults=dict(sample["参数默认值"]),
        expected_columns=list(sample["预期输出列"]),
        version_label=sample["模板版本"],
        revision="2026-11-15T02:00:00+00:00",
    )
    row = build_version_row(
        version_id=len(store.rows("sqltpl_version")) + 1,
        template_id=int(template["id"]),
        core=core,
        source="示例数据",
        actor="数据平台",
        status=VERSION_SUPERSEDED if superseded else "已发布",
        published_at="2026-11-15T02:00:00+00:00",
    )
    if superseded:
        row["superseded_at"] = "2026-11-16T02:00:00+00:00"
    store.rows("sqltpl_version").append(row)
    return row


def _seed_job_binding(job: dict[str, Any], version: dict[str, Any], parameters: dict[str, Any]) -> None:
    snapshot = version["快照"]
    rendered, unresolved = render_sql(snapshot["SQL片段"], parameters)
    if unresolved:
        return
    fingerprint = binding_fingerprint(int(job["id"]), version, parameters)
    binding = make_binding(
        job=job,
        version=version,
        parameters=parameters,
        rendered_sql=rendered,
        fingerprint=fingerprint,
    )
    template = store.find("sqltpl", int(version["模板ID"]))
    write_job_binding_fields(
        job,
        binding=binding,
        template=template or {},
        counterpart_job_no=None,
        version_is_latest=True,
    )
    job["模板ID"] = int(version["模板ID"])
    job["执行SQL"] = snapshot["SQL片段"]
    job["seed_note"] = "示例绑定：便于核对作业与模板版本的对应关系"


def bootstrap() -> None:
    """幂等：服务被重复导入或热重载时不会生成重复版本。"""
    if store.rows("sqltpl_version"):
        return
    with store.lock:
        _ensure_templates()
        templates = {str(row.get("模板编号")): row for row in store.rows("sqltpl")}
        first = templates.get("SQLT-0001")
        third = templates.get("SQLT-0003")
        if first is not None:
            sample = next(item for item in TEMPLATE_SAMPLES if item["模板编号"] == "SQLT-0001")
            version = _publish_seed_version(first, sample)
            touch_template_publish_fields(first, version)
        if third is not None:
            sample = next(item for item in TEMPLATE_SAMPLES if item["模板编号"] == "SQLT-0003")
            old_sample = dict(sample)
            old_sample["模板版本"] = "v1"
            old_sample["SQL片段"] = sample["SQL片段"].replace(
                "r.refund_amount", "r.refund_amount, '1' AS version_flag"
            )
            old = _publish_seed_version(third, old_sample, superseded=True)
            old["superseded_at"] = "2026-11-16T02:00:00+00:00"
            current = _publish_seed_version(third, sample)
            third["已发布版本数"] = 2
            touch_template_publish_fields(third, current)
            third["已发布版本数"] = 2

        jobs = store.rows("etljob")
        job = next((row for row in jobs if row.get("作业编号") == "ETLJ-0003"), None)
        if job is not None and first is not None:
            version = next(
                (row for row in store.rows("sqltpl_version") if int(row["模板ID"]) == int(first["id"])),
                None,
            )
            if version is not None:
                _seed_job_binding(
                    job,
                    version,
                    {"dt": "2026-11-15", "region_id": 1101},
                )
                job["模板发布状态"] = "已是最新"


bootstrap()
