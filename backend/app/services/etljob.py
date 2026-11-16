"""ETL作业业务规则：作业登记、模板版本绑定与状态流转。

作业定义只消费「已发布」或「已替代（历史）」的模板版本：
* 绑定前会按快照参数契约校验提交的参数，缺省值来自该版本快照；
* 同一作业重复提交同一份绑定（同版本 + 同参数）只生成一个绑定结果；
* 绑定完成后把版本号、快照摘要、渲染 SQL、参数绑定回写到作业清单。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.template_core import (
    diff_parameter_contract,
    normalize_bound_parameters,
    render_sql,
)
from app.template_repo import (
    VERSION_PUBLISHED,
    VERSION_SUPERSEDED,
    active_version,
    binding_detail,
    binding_fingerprint,
    bindings_of_job,
    current_binding,
    find_binding_by_fingerprint,
    find_template_by_no,
    find_version,
    is_bindable,
    make_binding,
    now_iso,
    version_summary,
    write_job_binding_fields,
)

MODULE = "etljob"
REQUIRED_FIELDS = ["作业编号", "作业名称", "源端数据表"]
STATUS_ORDER = ["草稿", "作业评审中", "已发布", "已下线"]
ACTION_RULES = {"提交作业评审": "作业评审中", "发布作业": "已发布", "下线作业": "已下线"}
NEGATIVE_ACTIONS = ["下线作业"]


class JobBindingError(Exception):
    def __init__(self, message: str, *, status_code: int = 400, entry: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.entry = entry


class EtljobService:
    # ------------------------------------------------------------------ 列表与登记
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("作业编号", "")) or keyword in str(row.get("作业名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def _decorate(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = dict(job)
        if payload.get("模板版本ID"):
            active = active_version(int(payload.get("模板ID") or 0)) if payload.get("模板ID") else None
            if active is None:
                template = find_template_by_no(str(payload.get("模板编号") or ""))
                active = active_version(int(template["id"])) if template else None
            payload["模板最新发布版本"] = active.get("模板版本") if active else payload.get("模板发布版本")
            payload["模板发布状态"] = (
                "已是最新"
                if active and int(active["id"]) == int(payload.get("模板版本ID") or 0)
                else "发现新版本"
            )
        else:
            payload.setdefault("绑定状态", "未绑定")
            payload.setdefault("模板发布状态", "未绑定模板")
        return payload

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        job = store.find(MODULE, entry_id)
        if job is None:
            return None
        payload = self._decorate(job)
        binding = current_binding(entry_id)
        payload["当前绑定"] = (
            binding_detail(binding, find_template_by_no(str(job.get("模板编号") or "")))
            if binding
            else None
        )
        payload["绑定历史"] = [
            {
                "绑定ID": row.get("id"),
                "模板版本": row.get("模板版本"),
                "模板版本ID": row.get("版本ID"),
                "快照摘要": row.get("快照摘要"),
                "参数绑定": row.get("参数绑定"),
                "绑定时间": row.get("绑定时间"),
                "绑定状态": row.get("绑定状态"),
            }
            for row in bindings_of_job(entry_id)
        ]
        return payload

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        job_no = str(values.get("作业编号")).strip()
        rows = store.rows(MODULE)
        if any(str(row.get("作业编号")) == job_no for row in rows):
            return None, [f"作业编号「{job_no}」已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry.setdefault("模板编号", values.get("模板编号"))
        entry["绑定状态"] = "未绑定"
        entry["模板发布状态"] = "未绑定模板"
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"作业定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于ETL作业可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "发布作业" and not entry.get("绑定ID"):
            return None, "作业尚未绑定已发布的模板版本，不能发布"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"作业定义已{action}"

    # ------------------------------------------------------------------ 模板版本绑定
    def list_available_versions(self, template_id: int) -> list[dict[str, Any]]:
        template = store.find("sqltpl", template_id)
        if template is None:
            raise JobBindingError(f"模板定义 {template_id} 不存在或已归档", status_code=404)
        rows = [
            row
            for row in store.rows("sqltpl_version")
            if int(row.get("模板ID", 0)) == template_id
            and row.get("status") in (VERSION_PUBLISHED, VERSION_SUPERSEDED)
        ]
        rows.sort(key=lambda row: int(row["id"]), reverse=True)
        return [version_summary(row) for row in rows]

    def _resolve_version(
        self,
        job: dict[str, Any],
        template_id: Any,
        version_id: Any,
        version_label: Any,
        template_no_value: Any = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """只解析已发布（或已替代）的版本，草稿一律不可绑定。"""
        if template_id in (None, "", 0):
            template_no = str(template_no_value or job.get("模板编号") or "").strip()
            if not template_no:
                raise JobBindingError(
                    "必须提供模板ID或模板编号，并选择已发布的模板版本",
                    status_code=400,
                )
            template = find_template_by_no(template_no)
            if template is None:
                raise JobBindingError(f"模板编号「{template_no}」不存在", status_code=404)
        else:
            template = store.find("sqltpl", int(template_id))
            if template is None:
                raise JobBindingError(f"模板定义 {template_id} 不存在或已归档", status_code=404)

        if template.get("status") == "已废弃":
            raise JobBindingError(
                f"模板 {template.get('模板编号')} 已废弃，不能再绑定新作业",
                status_code=409,
            )

        version: dict[str, Any] | None = None
        if version_id not in (None, "", 0):
            version = find_version(int(template["id"]), int(version_id))
            if version is None:
                raise JobBindingError(
                    f"版本 {version_id} 不属于模板 {template.get('模板编号')}", status_code=404
                )
        elif version_label not in (None, ""):
            for row in store.rows("sqltpl_version"):
                if (
                    int(row.get("模板ID", 0)) == int(template["id"])
                    and str(row.get("模板版本")) == str(version_label)
                ):
                    version = row
                    break
            if version is None:
                raise JobBindingError(
                    f"模板 {template.get('模板编号')} 不存在版本「{version_label}」", status_code=404
                )
        else:
            version = active_version(int(template["id"]))
            if version is None:
                raise JobBindingError(
                    f"模板 {template.get('模板编号')} 尚未发布任何版本，作业不能绑定",
                    status_code=409,
                )

        if not is_bindable(version):
            raise JobBindingError(
                f"版本「{version.get('模板版本')}」状态为「{version.get('status')}」，只有已发布或已替代版本可绑定",
                status_code=409,
            )
        return template, version

    def _resolve_parameters(
        self,
        version: dict[str, Any],
        raw_parameters: Any,
        *,
        strict: bool,
    ) -> dict[str, Any]:
        snapshot = version.get("快照") or {}
        placeholders = list(snapshot.get("参数占位符") or [])
        defaults = dict(snapshot.get("参数默认值") or {})
        supplied, errors = normalize_bound_parameters(raw_parameters)
        if errors:
            raise JobBindingError("；".join(errors))
        unknown = [key for key in supplied if key not in placeholders]
        if unknown and strict:
            raise JobBindingError(
                "参数绑定包含模板版本未声明的占位符：" + "、".join(unknown)
            )
        values: dict[str, Any] = {}
        missing: list[str] = []
        for name in placeholders:
            if name in supplied:
                values[name] = supplied[name]
            elif name in defaults:
                values[name] = defaults[name]
            else:
                missing.append(name)
        if missing:
            raise JobBindingError(
                "参数绑定缺少占位符且版本未提供默认值：" + "、".join(missing)
            )
        rendered, unresolved = render_sql(str(snapshot.get("SQL片段") or ""), values)
        if unresolved:
            raise JobBindingError("SQL渲染后仍有未解析占位符：" + "、".join(unresolved))
        return {"values": values, "rendered": rendered, "unknown": unknown}

    def bind_template_version(
        self,
        job_id: int,
        values: dict[str, Any],
        *,
        actor: str = "值班管理员",
        strict: bool = True,
    ) -> dict[str, Any]:
        """把作业绑定到不可变模板版本；同一请求只生成一个绑定结果。"""
        with store.lock:
            job = store.find(MODULE, job_id)
            if job is None:
                raise JobBindingError(f"作业定义 {job_id} 不存在或已归档", status_code=404)
            if job.get("status") == "已下线":
                raise JobBindingError(f"作业 {job.get('作业编号')} 已下线，不能重新绑定模板", status_code=409)
            template, version = self._resolve_version(
                job,
                values.get("模板ID"),
                values.get("模板版本ID"),
                values.get("模板版本"),
                values.get("模板编号"),
            )
            resolved = self._resolve_parameters(version, values.get("参数绑定"), strict=strict)
            parameters = resolved["values"]
            fingerprint = binding_fingerprint(job_id, version, parameters)

            existing = find_binding_by_fingerprint(job_id, fingerprint)
            counterpart_job_no = str(values.get("对侧作业编号") or "").strip() or None
            if existing is not None:
                if counterpart_job_no:
                    self._link_counterpart(job, existing, counterpart_job_no)
                write_job_binding_fields(
                    job,
                    binding=existing,
                    template=template,
                    counterpart_job_no=existing.get("对侧作业编号"),
                    version_is_latest=bool(
                        active_version(int(template["id"]))
                        and int(active_version(int(template["id"]))["id"]) == int(existing["版本ID"])
                    ),
                )
                return {
                    "binding": binding_detail(existing, template),
                    "idempotent": True,
                    "message": f"作业已绑定版本「{existing.get('模板版本')}」且参数一致，复用原绑定",
                }

            binding = make_binding(
                job=job,
                version=version,
                parameters=parameters,
                rendered_sql=resolved["rendered"],
                fingerprint=fingerprint,
                counterpart_job_no=counterpart_job_no,
            )
            if counterpart_job_no:
                self._link_counterpart(job, binding, counterpart_job_no)
            active = active_version(int(template["id"]))
            write_job_binding_fields(
                job,
                binding=binding,
                template=template,
                counterpart_job_no=binding.get("对侧作业编号"),
                version_is_latest=bool(active and int(active["id"]) == int(binding["版本ID"])),
            )
            job["模板ID"] = int(template["id"])
            job["abnormal"] = bool(
                version.get("参数冲突") and binding.get("版本ID") != (active or {}).get("id")
            )
            return {
                "binding": binding_detail(binding, template),
                "idempotent": False,
                "message": f"作业已绑定版本「{binding.get('模板版本')}」并生成参数绑定",
            }

    def _link_counterpart(self, job: dict[str, Any], binding: dict[str, Any], counterpart_job_no: str) -> None:
        """对侧业务面的作业定义沿用同一版本：按作业编号找到对面并写回一致版本。"""
        counterpart = None
        for row in store.rows(MODULE):
            if str(row.get("作业编号")) == counterpart_job_no:
                counterpart = row
                break
        if counterpart is None:
            raise JobBindingError(
                f"对侧作业编号「{counterpart_job_no}」不存在", status_code=404
            )
        if int(counterpart["id"]) == int(job["id"]):
            raise JobBindingError("对侧作业不能是当前作业本身", status_code=400)
        if not counterpart.get("绑定ID"):
            raise JobBindingError(
                f"对侧作业「{counterpart_job_no}」尚未绑定模板版本，不能沿用当前版本",
                status_code=409,
            )
        if int(counterpart.get("模板版本ID") or 0) != int(binding["版本ID"]):
            raise JobBindingError(
                f"对侧作业「{counterpart_job_no}」绑定的是版本 {counterpart.get('模板版本')}，"
                f"与当前版本 {binding.get('模板版本')} 不一致",
                status_code=409,
            )
        binding["对侧作业编号"] = counterpart_job_no
        binding["对侧绑定ID"] = counterpart.get("绑定ID")
        counterpart["对侧作业编号"] = job.get("作业编号")
        counterpart["对侧绑定ID"] = binding.get("id")
        counterpart["对侧绑定版本"] = binding.get("模板版本")
        counterpart["对侧版本一致"] = True

    def job_binding(self, job_id: int) -> dict[str, Any]:
        job = store.find(MODULE, job_id)
        if job is None:
            raise JobBindingError(f"作业定义 {job_id} 不存在或已归档", status_code=404)
        binding = current_binding(job_id)
        if binding is None:
            raise JobBindingError(f"作业 {job.get('作业编号')} 尚未绑定模板版本", status_code=404)
        template = find_template_by_no(str(binding.get("模板编号") or ""))
        return {
            "job": self._decorate(job),
            "binding": binding_detail(binding, template),
            "history": [
                {
                    "绑定ID": row.get("id"),
                    "模板版本": row.get("模板版本"),
                    "快照摘要": row.get("快照摘要"),
                    "参数绑定摘要": row.get("参数绑定摘要"),
                    "绑定时间": row.get("绑定时间"),
                }
                for row in bindings_of_job(job_id)
            ],
        }

    def list_bindings(
        self,
        *,
        job_id: int | None = None,
        template_no: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows("etljob_binding")
        if job_id is not None:
            rows = [row for row in rows if int(row.get("作业ID", 0)) == job_id]
        if template_no:
            rows = [row for row in rows if str(row.get("模板编号")) == template_no]
        rows = sorted(rows, key=lambda row: int(row["id"]), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        items: list[dict[str, Any]] = []
        for row in rows[start:start + size]:
            template = find_template_by_no(str(row.get("模板编号") or ""))
            items.append(binding_detail(row, template))
        return items, total

    def version_impact(self, template_id: int, version_id: int) -> dict[str, Any]:
        """发布前的版本影响面：哪些作业仍绑在旧版本、参数契约差异是什么。"""
        version = find_version(template_id, version_id)
        if version is None:
            raise JobBindingError(f"模板版本 {version_id} 不属于模板 {template_id}", status_code=404)
        active = active_version(template_id)
        conflicts = diff_parameter_contract(
            active.get("快照") if active else None, version.get("快照") or {}
        )
        impact = []
        for row in store.rows("etljob_binding"):
            if int(row.get("模板ID", 0)) != int(template_id):
                continue
            if row.get("绑定状态") != "已绑定":
                continue
            if int(row.get("版本ID", 0)) == int(version_id):
                continue
            impact.append(
                {
                    "作业ID": row.get("作业ID"),
                    "作业编号": row.get("作业编号"),
                    "绑定版本": row.get("模板版本"),
                    "绑定ID": row.get("id"),
                    "绑定时间": row.get("绑定时间"),
                }
            )
        return {
            "模板ID": template_id,
            "模板编号": version.get("模板编号"),
            "待发布版本": version.get("模板版本"),
            "当前发布版本": active.get("模板版本") if active else None,
            "参数冲突": conflicts,
            "冲突占位符": [
                name
                for name in version.get("参数占位符") or []
                if name not in ((active or {}).get("参数默认值") or {})
            ],
            "旧版本绑定作业数": len(impact),
            "旧版本绑定作业": impact,
            "说明": "发布只影响新绑定；已绑定的作业继续使用其原版本快照",
        }

    def update_job_after_template_publish(self, template_id: int, published_version_id: int) -> int:
        """模板发布后的状态回写：作业列表只标记「发现新版本」，不自动改绑。"""
        template = store.find("sqltpl", template_id)
        if template is None:
            return 0
        touched = 0
        for job in store.rows(MODULE):
            if int(job.get("模板ID") or 0) != int(template_id):
                continue
            if job.get("绑定ID"):
                job["模板发布状态"] = (
                    "已是最新" if int(job.get("模板版本ID") or 0) == int(published_version_id) else "发现新版本"
                )
                job["模板最新发布版本"] = template.get("当前发布版本")
                job["最新快照摘要"] = template.get("当前发布快照摘要")
            else:
                job["模板发布状态"] = "待绑定已发布版本"
                job["模板最新发布版本"] = template.get("当前发布版本")
            job["最近发布检查时间"] = now_iso()
            touched += 1
        return touched
