"""SQL模板业务规则：模板登记、版本发布、文件导入导出与发布互斥。

关键约束：
* 模板版本发布后不可修改，快照摘要参与作业绑定指纹；
* 同一模板同一时刻只允许一个「已发布」版本，发布是原子的；
* 重复提交同一份内容（含导入）只返回已有版本，不生成重复结果；
* 发布某个版本会把该模板此前的「已发布」版本标成「已替代」，但历史作业
  绑定仍指向原版本，不会被改写。
"""
from __future__ import annotations

import json
from typing import Any

from app.store import store
from app.template_core import (
    build_core_snapshot,
    content_signature,
    diff_parameter_contract,
    normalize_defaults,
    normalize_expected_columns,
    normalize_placeholders,
    sha256_bytes,
    snapshot_digest,
    validate_placeholders_against_sql,
    version_file_name,
)
from app.template_repo import (
    VERSION_DRAFT,
    VERSION_PUBLISHED,
    VERSION_SUPERSEDED,
    active_version,
    find_template_by_no,
    find_version,
    find_version_by_label,
    freeze_version,
    make_version,
    next_id,
    now_iso,
    touch_template_publish_fields,
    version_summary,
    versions_of,
)

MODULE = "sqltpl"
REQUIRED_FIELDS = ["模板编号", "模板名称", "模板类型"]
STATUS_ORDER = ["草稿", "模板评审中", "已发布", "已废弃"]
ACTION_RULES = {"提交模板评审": "模板评审中", "发布模板": "已发布", "废弃模板": "已废弃"}
NEGATIVE_ACTIONS = []

IMPORT_FORMAT = "etl-sqltpl-version"


class TemplateError(Exception):
    """带上 HTTP 语义的业务错误，路由层直接映射成响应。

    ``reusable=True`` 表示「内容与已有版本一致」，调用方（导入）可以把它当作
    复用而不是失败；同名不同内容的 409 不应被当作复用。
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        entry: dict[str, Any] | None = None,
        reusable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.entry = entry
        self.reusable = reusable


def _version_order(version: dict[str, Any]) -> tuple[int, str]:
    """版本先后：优先按版本 ID（导入顺序），同 ID 时按版本号兜底。"""
    return int(version.get("id") or 0), str(version.get("模板版本") or "")


def normalize_version_spec(
    *,
    template_no: str,
    version_label: Any,
    revision: Any = None,
    template_name: str = "",
    template_type: str = "",
    sql_fragment: Any = "",
    placeholders: Any = None,
    defaults: Any = None,
    expected_columns: Any = None,
) -> tuple[dict[str, Any], list[str]]:
    """把一次版本提交规范化成不可变快照核心；所有冲突在这里一次说清。"""
    errors: list[str] = []
    label = str(version_label or "").strip()
    if not label:
        errors.append("模板版本不能为空")
    sql = str(sql_fragment or "")
    if not sql.strip():
        errors.append("SQL片段不能为空")

    declared, placeholder_errors = normalize_placeholders(placeholders)
    errors.extend(placeholder_errors)
    errors.extend(validate_placeholders_against_sql(sql, declared))
    normalized_defaults, default_errors = normalize_defaults(defaults, declared)
    errors.extend(default_errors)
    columns, column_errors = normalize_expected_columns(expected_columns)
    errors.extend(column_errors)
    if errors:
        return {}, errors

    revision_text = str(revision or "").strip() or now_iso()
    core = build_core_snapshot(
        template_no=template_no,
        template_name=template_name,
        template_type=template_type,
        sql_fragment=sql,
        placeholders=declared,
        defaults=normalized_defaults,
        expected_columns=columns,
        version_label=label,
        revision=revision_text,
    )
    return core, []


class SqltplService:
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
                if keyword in str(row.get("模板编号", "")) or keyword in str(row.get("模板名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._decorate(row) for row in rows[start:start + size]], total

    def _decorate(self, template: dict[str, Any]) -> dict[str, Any]:
        payload = dict(template)
        active = active_version(int(template["id"]))
        payload["当前发布版本"] = active.get("模板版本") if active else template.get("当前发布版本")
        payload["当前发布版本ID"] = active.get("id") if active else template.get("当前发布版本ID")
        payload["当前发布快照摘要"] = active.get("快照摘要") if active else template.get("当前发布快照摘要")
        payload["版本总数"] = len(versions_of(int(template["id"])))
        payload["草稿版本数"] = sum(
            1 for row in versions_of(int(template["id"])) if row.get("status") == VERSION_DRAFT
        )
        return payload

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        template = store.find(MODULE, entry_id)
        if template is None:
            return None
        payload = self._decorate(template)
        payload["versions"] = [version_summary(row) for row in versions_of(entry_id)]
        return payload

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        template_no = str(values.get("模板编号")).strip()
        rows = store.rows(MODULE)
        if any(str(row.get("模板编号")) == template_no for row in rows):
            return None, [f"模板编号「{template_no}」已存在"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["当前发布版本"] = None
        entry["当前发布版本ID"] = None
        entry["当前发布快照摘要"] = None
        entry["已发布版本数"] = 0
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"模板定义 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于SQL模板可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 「发布模板」不能直接把模板改成已发布：发布必须落到某个不可变版本上，
        # 否则作业会绑定到没有快照的「已发布」状态。
        if action == "发布模板":
            return None, "请在版本列表中发布具体版本（发布模板版本），模板状态由版本发布自动维护"
        if action == "废弃模板" and active_version(entry_id) is not None:
            return None, "模板仍存在已发布版本，请先确认作业全部迁移或下线后再废弃"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"模板定义已{action}"

    # ------------------------------------------------------------------ 版本创建
    def _resolve_template(self, template_id: int) -> dict[str, Any]:
        template = store.find(MODULE, template_id)
        if template is None:
            raise TemplateError(f"模板定义 {template_id} 不存在或已归档", status_code=404)
        if template.get("status") == "已废弃":
            raise TemplateError(f"模板 {template.get('模板编号')} 已废弃，不能继续提交版本", status_code=409)
        return template

    def _base_version(self, template_id: int, base_version_id: Any) -> dict[str, Any] | None:
        if base_version_id in (None, "", 0):
            return active_version(template_id)
        version = find_version(template_id, int(base_version_id))
        if version is None:
            raise TemplateError(f"基线版本 {base_version_id} 不属于该模板", status_code=404)
        return version

    def create_version(
        self,
        template_id: int,
        values: dict[str, Any],
        *,
        source: str = "手工提交",
        actor: str = "值班管理员",
        import_source: str | None = None,
        require_new_content: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """创建草稿版本；返回 (版本, 是否新建)。完全重复的内容返回已有版本。"""
        with store.lock:
            template = self._resolve_template(template_id)
            base = self._base_version(template_id, values.get("基线版本ID"))
            sql = values.get("SQL片段")
            if sql in (None, "") and base is not None:
                sql = base.get("SQL片段")
            placeholders = values.get("参数占位符")
            if placeholders is None and base is not None:
                placeholders = base.get("参数占位符")
            defaults = values.get("参数默认值")
            if defaults is None and base is not None:
                defaults = base.get("参数默认值")
            expected = values.get("预期输出列")
            if expected is None and base is not None:
                expected = base.get("预期输出列")

            core, errors = normalize_version_spec(
                template_no=str(template.get("模板编号")),
                version_label=values.get("模板版本"),
                revision=values.get("修订号"),
                template_name=str(values.get("模板名称") or template.get("模板名称") or ""),
                template_type=str(values.get("模板类型") or template.get("模板类型") or ""),
                sql_fragment=sql,
                placeholders=placeholders,
                defaults=defaults,
                expected_columns=expected,
            )
            if errors:
                raise TemplateError("；".join(errors))

            label = core["模板版本"]
            existing_label = find_version_by_label(template_id, label)
            if existing_label is not None:
                if existing_label.get("快照摘要") == snapshot_digest(core):
                    raise TemplateError(
                        f"版本「{label}」已存在且内容一致，未生成新版本",
                        status_code=409,
                        entry=version_summary(existing_label),
                        reusable=True,
                    )
                raise TemplateError(
                    f"版本「{label}」已存在且内容不同，请换一个版本号",
                    status_code=409,
                    entry=version_summary(existing_label),
                )

            signature = content_signature(
                template_no=core["模板编号"],
                sql_fragment=core["SQL片段"],
                placeholders=core["参数占位符"],
                defaults=core["参数默认值"],
                expected_columns=core["预期输出列"],
            )
            for row in versions_of(template_id):
                if row.get("内容签名") == signature:
                    raise TemplateError(
                        f"版本内容与已有版本「{row.get('模板版本')}」完全一致，未生成重复版本",
                        status_code=409,
                        entry=version_summary(row),
                        reusable=True,
                    )
            if require_new_content and base is not None and base.get("内容签名") == signature:
                raise TemplateError(
                    "导入内容与当前发布版本一致，无需生成新版本",
                    status_code=409,
                    entry=version_summary(base),
                    reusable=True,
                )

            version = make_version(
                template=template,
                core=core,
                source=source,
                actor=actor,
                base_version_id=int(base["id"]) if base else None,
                import_source=import_source,
            )
            version["参数冲突"] = diff_parameter_contract(
                base.get("快照") if base else None, core
            )
            version["abnormal"] = bool(version["参数冲突"])
            template["待发布版本"] = label
            template["待发布版本ID"] = version["id"]
            if template.get("status") not in ("已发布",):
                template["status"] = "模板评审中"
                template["pending"] = True
            return version, True

    # ------------------------------------------------------------------ 发布
    def publish_version(
        self, template_id: int, version_id: int, *, expected_digest: str | None = None
    ) -> dict[str, Any]:
        """原子发布：同一模板只允许一个已发布版本，重复发布同一版本幂等。"""
        with store.lock:
            template = self._resolve_template(template_id)
            version = find_version(template_id, version_id)
            if version is None:
                raise TemplateError(f"模板版本 {version_id} 不属于模板 {template_id}", status_code=404)
            if version.get("status") == VERSION_PUBLISHED:
                return {
                    "version": version_summary(version),
                    "idempotent": True,
                    "message": f"版本「{version.get('模板版本')}」已经是当前发布版本",
                }
            if version.get("status") == VERSION_SUPERSEDED:
                raise TemplateError(
                    f"版本「{version.get('模板版本')}」已被替代，历史作业仍可继续绑定，但不能重新发布",
                    status_code=409,
                )
            if expected_digest and expected_digest != version.get("快照摘要"):
                raise TemplateError("提交的版本摘要与当前草稿不一致，请刷新后重试", status_code=409)

            try:
                freeze_version(version)
            except ValueError as exc:
                raise TemplateError(str(exc), status_code=409) from exc

            # 冻结成功后再降级旧版本：两步都在同一把锁里，失败时旧发布版本不受影响。
            for row in versions_of(template_id):
                if row.get("status") == VERSION_PUBLISHED and int(row["id"]) != int(version_id):
                    row["status"] = VERSION_SUPERSEDED
                    row["superseded_at"] = now_iso()
                    row["pending"] = False

            touch_template_publish_fields(template, version)
            self._writeback_jobs(template)
            return {
                "version": version_summary(version),
                "idempotent": False,
                "message": f"版本「{version.get('模板版本')}」已发布",
            }

    def _writeback_jobs(self, template: dict[str, Any]) -> int:
        """模板发布后回写作业清单状态；延迟导入避免服务层循环依赖。"""
        from app.services.etljob import EtljobService

        return EtljobService().update_job_after_template_publish(
            int(template["id"]), int(template.get("当前发布版本ID") or 0)
        )

    def publish_latest_draft(self, template_id: int, *, expected_digest: str | None = None) -> dict[str, Any]:
        drafts = [row for row in versions_of(template_id) if row.get("status") == VERSION_DRAFT]
        if not drafts:
            raise TemplateError("该模板没有待发布的草稿版本", status_code=409)
        latest = max(drafts, key=lambda row: int(row["id"]))
        return self.publish_version(template_id, int(latest["id"]), expected_digest=expected_digest)

    # ------------------------------------------------------------------ 版本查询与文件
    def list_versions(
        self,
        template_id: int,
        *,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        template = store.find(MODULE, template_id)
        if template is None:
            raise TemplateError(f"模板定义 {template_id} 不存在或已归档", status_code=404)
        rows = versions_of(template_id)
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows = sorted(rows, key=lambda row: int(row["id"]), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return [version_summary(row) for row in rows[start:start + size]], total

    def version_detail(self, template_id: int, version_id: int) -> dict[str, Any]:
        version = find_version(template_id, version_id)
        if version is None:
            raise TemplateError(f"模板版本 {version_id} 不属于模板 {template_id}", status_code=404)
        payload = version_summary(version)
        payload["快照"] = version.get("快照")
        return payload

    def version_file(self, template_id: int, version_id: int) -> tuple[str, bytes]:
        version = find_version(template_id, version_id)
        if version is None:
            raise TemplateError(f"模板版本 {version_id} 不属于模板 {template_id}", status_code=404)
        file_name = version_file_name(str(version.get("模板编号")), str(version.get("模板版本")))
        return file_name, str(version.get("SQL片段") or "").encode("utf-8")

    def export_bundle(self) -> dict[str, Any]:
        """导出全部已发布/已替代版本，形成可再次导入的版本包。"""
        items: list[dict[str, Any]] = []
        for template in store.rows(MODULE):
            for version in versions_of(int(template["id"])):
                if version.get("status") not in (VERSION_PUBLISHED, VERSION_SUPERSEDED):
                    continue
                summary = version_summary(version)
                summary.pop("id", None)
                summary.pop("模板ID", None)
                items.append(summary)
        return {
            "format": IMPORT_FORMAT,
            "generated_at": now_iso(),
            "total": len(items),
            "versions": items,
        }

    def export_bundle_bytes(self) -> tuple[str, bytes]:
        return "sqltpl-version-bundle.json", json.dumps(
            self.export_bundle(), ensure_ascii=False, indent=2
        ).encode("utf-8")

    # ------------------------------------------------------------------ 导入 / 上传
    def import_bundle(
        self,
        raw: bytes,
        *,
        actor: str = "值班管理员",
        file_name: str = "bundle.json",
        template_no_override: str | None = None,
    ) -> dict[str, Any]:
        """导入版本包：内容已存在则复用，不重复生成；SQL 片段逐字保留。"""
        if not raw:
            raise TemplateError("导入文件为空", status_code=400)
        digest = sha256_bytes(raw)
        try:
            payload = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TemplateError(f"导入文件不是合法 JSON：{exc}", status_code=400) from exc

        if isinstance(payload, list):
            payload = {"format": IMPORT_FORMAT, "versions": payload}
        if not isinstance(payload, dict):
            raise TemplateError("导入文件顶层必须是对象或数组", status_code=400)
        if payload.get("format") and payload.get("format") != IMPORT_FORMAT:
            raise TemplateError(
                f"不支持的导入格式「{payload.get('format')}」，期望 {IMPORT_FORMAT}",
                status_code=400,
            )
        versions = payload.get("versions")
        if not isinstance(versions, list) or not versions:
            raise TemplateError("导入文件缺少 versions 数组或内容为空", status_code=400)
        if len(versions) > 500:
            raise TemplateError("单次最多导入 500 个版本，请拆分文件", status_code=400)

        imported: list[dict[str, Any]] = []
        reused: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, item in enumerate(versions, start=1):
            try:
                result = self._import_one(
                    item,
                    actor=actor,
                    file_name=file_name,
                    digest=digest,
                    index=index,
                    template_no_override=template_no_override,
                )
            except TemplateError as exc:
                if exc.reusable and exc.entry is not None:
                    reused.append({"index": index, "reason": exc.message, "version": exc.entry})
                    continue
                errors.append(f"第 {index} 个版本：{exc.message}")
                continue
            imported.append(result)
        if errors:
            raise TemplateError("；".join(errors), status_code=400)
        return {
            "imported": imported,
            "reused": reused,
            "imported_count": len(imported),
            "reused_count": len(reused),
            "file": file_name,
            "file_digest": digest,
        }

    def _import_one(
        self,
        item: Any,
        *,
        actor: str,
        file_name: str,
        digest: str,
        index: int,
        template_no_override: str | None,
    ) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise TemplateError("版本条目必须是对象")
        snapshot = item.get("快照") if isinstance(item.get("快照"), dict) else item
        template_no = str(
            template_no_override or item.get("模板编号") or snapshot.get("模板编号") or ""
        ).strip()
        if not template_no:
            raise TemplateError("版本条目缺少模板编号")
        version_label = item.get("模板版本") or snapshot.get("模板版本")
        template = find_template_by_no(template_no)
        if template is None:
            template = {
                "id": next_id(store.rows(MODULE)),
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "模板编号": template_no,
                "模板名称": str(snapshot.get("模板名称") or template_no),
                "模板类型": str(snapshot.get("模板类型") or "导入模板"),
                "当前发布版本": None,
                "当前发布版本ID": None,
                "当前发布快照摘要": None,
                "已发布版本数": 0,
            }
            store.rows(MODULE).append(template)

        version, _created = self.create_version(
            int(template["id"]),
            {
                "模板版本": version_label,
                "修订号": item.get("修订号") or snapshot.get("修订号"),
                "SQL片段": snapshot.get("SQL片段"),
                "参数占位符": snapshot.get("参数占位符"),
                "参数默认值": snapshot.get("参数默认值"),
                "预期输出列": snapshot.get("预期输出列"),
                "模板名称": snapshot.get("模板名称"),
                "模板类型": snapshot.get("模板类型"),
            },
            source="版本包导入",
            actor=actor,
            import_source=f"{file_name}#{digest[:12]}#{index}",
            require_new_content=True,
        )
        # 导入的已发布版本保持「已发布」语义：进入草稿后立即发布，但不抢占
        # 比它更新的当前发布版本（导入历史版本时只登记为可绑定版本）。
        target_status = item.get("status")
        if target_status in (VERSION_PUBLISHED, VERSION_SUPERSEDED):
            active = active_version(int(template["id"]))
            if active is None or _version_order(version) >= _version_order(active):
                self.publish_version(int(template["id"]), int(version["id"]))
            else:
                version["status"] = VERSION_SUPERSEDED
                version["pending"] = False
                version["superseded_at"] = now_iso()
        return {
            "模板编号": template_no,
            "模板版本": version.get("模板版本"),
            "版本ID": version.get("id"),
            "快照摘要": version.get("快照摘要"),
            "status": version.get("status"),
            "导入来源": version.get("导入来源"),
        }

    def upload_version_file(
        self,
        template_id: int,
        *,
        raw: bytes,
        file_name: str,
        version_label: str | None = None,
        placeholders: str | None = None,
        defaults: str | None = None,
        expected_columns: str | None = None,
        actor: str = "值班管理员",
    ) -> dict[str, Any]:
        """上传新 SQL 文件生成草稿版本；与当前发布内容相同则不生成重复版本。"""
        if not raw:
            raise TemplateError("上传文件为空", status_code=400)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise TemplateError("上传文件必须是 UTF-8 编码的 SQL 文本", status_code=400) from exc
        with store.lock:
            template = self._resolve_template(template_id)
            base = active_version(template_id)
            label = (version_label or "").strip()
            if not label:
                label = f"v{len(versions_of(template_id)) + 1}"
            if placeholders is not None and str(placeholders).strip():
                declared_raw: Any = placeholders
            elif base is not None:
                declared_raw = base.get("参数占位符")
            else:
                declared_raw = None
            defaults_raw: Any = defaults if defaults not in (None, "") else (
                base.get("参数默认值") if base is not None else None
            )
            expected_raw: Any = expected_columns if expected_columns not in (None, "") else (
                base.get("预期输出列") if base is not None else None
            )

            core, errors = normalize_version_spec(
                template_no=str(template.get("模板编号")),
                version_label=label,
                template_name=str(template.get("模板名称") or ""),
                template_type=str(template.get("模板类型") or ""),
                sql_fragment=text,
                placeholders=declared_raw,
                defaults=defaults_raw,
                expected_columns=expected_raw,
            )
            if errors:
                raise TemplateError("；".join(errors))
            signature = content_signature(
                template_no=core["模板编号"],
                sql_fragment=core["SQL片段"],
                placeholders=core["参数占位符"],
                defaults=core["参数默认值"],
                expected_columns=core["预期输出列"],
            )
            for row in versions_of(template_id):
                if row.get("内容签名") == signature:
                    return {
                        "version": version_summary(row),
                        "created": False,
                        "idempotent": True,
                        "message": f"上传内容与已有版本「{row.get('模板版本')}」一致，复用该版本",
                    }
            if find_version_by_label(template_id, label) is not None:
                raise TemplateError(f"版本「{label}」已存在且内容不同，请换一个版本号", status_code=409)
            version = make_version(
                template=template,
                core=core,
                source="文件上传",
                actor=actor,
                base_version_id=int(base["id"]) if base else None,
                import_source=file_name,
            )
            version["参数冲突"] = diff_parameter_contract(base.get("快照") if base else None, core)
            version["abnormal"] = bool(version["参数冲突"])
            template["待发布版本"] = label
            template["待发布版本ID"] = version["id"]
            if template.get("status") not in ("已发布",):
                template["status"] = "模板评审中"
                template["pending"] = True
            return {
                "version": version_summary(version),
                "created": True,
                "idempotent": False,
                "message": f"已从文件 {file_name} 生成草稿版本「{label}」",
            }
