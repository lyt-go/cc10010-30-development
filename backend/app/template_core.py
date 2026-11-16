"""SQL模板版本与参数绑定的纯函数。

这一层不依赖内存数据仓库，方便示例数据、服务和测试复用同一套
占位符解析、默认值规范化、快照摘要与 SQL 渲染规则。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DECLARED_WRAPPER_RE = re.compile(r"^\s*(?:\{\{|\$\{)\s*(.*?)\s*(?:\}\}|\})\s*$")
_SPLIT_RE = re.compile(r"[,，;；\s]+")


def canonical_json(value: Any) -> str:
    """稳定序列化：快照摘要必须不受字典插入顺序影响。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_placeholder_name(raw: Any) -> str:
    """把 ``{{ dt }}``、``${dt}``、``dt`` 统一成 ``dt``。"""
    text = str(raw if raw is not None else "").strip()
    matched = _DECLARED_WRAPPER_RE.match(text)
    if matched:
        text = matched.group(1)
    return text.strip()


def discover_placeholders(sql: str) -> list[str]:
    """按出现顺序返回 SQL 中实际使用的占位符（同一个占位符只记一次）。"""
    seen: list[str] = []
    for match in PLACEHOLDER_RE.finditer(sql or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def normalize_placeholders(raw: Any) -> tuple[list[str], list[str]]:
    """把多种提交形态的占位符声明规范化。

    支持列表、JSON 数组字符串、逗号/空白分隔字符串，也支持
    ``[{"名称": "dt", "默认值": ...}]`` 这种带默认值的写法（默认值由
    ``normalize_defaults`` 统一处理）。重复声明会明确报错，而不是静默去重。
    """
    errors: list[str] = []
    items: list[Any] = []

    if raw is None or raw == "":
        return [], []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return [], [f"参数占位符不是合法 JSON：{exc.msg}"]
            if not isinstance(parsed, list):
                return [], ["参数占位符 JSON 必须是数组"]
            items = parsed
        else:
            items = [part for part in _SPLIT_RE.split(text) if part]
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        return [], ["参数占位符必须是数组、JSON 数组字符串或分隔字符串"]

    names: list[str] = []
    positions: dict[str, int] = {}
    for index, item in enumerate(items, start=1):
        if isinstance(item, dict):
            raw_name = (
                item.get("名称")
                or item.get("参数名")
                or item.get("占位符")
                or item.get("name")
                or ""
            )
        else:
            raw_name = item
        name = normalize_placeholder_name(raw_name)
        if not name:
            errors.append(f"第 {index} 个参数占位符为空")
            continue
        if not NAME_RE.match(name):
            errors.append(f"参数占位符「{name}」不合法，只允许字母、数字和下划线且不能以数字开头")
            continue
        if name in positions:
            errors.append(
                f"参数占位符「{name}」重复声明（第 {positions[name]} 项与第 {index} 项）"
            )
            continue
        positions[name] = index
        names.append(name)
    return names, errors


def validate_placeholders_against_sql(sql: str, declared: list[str]) -> list[str]:
    """声明集合必须与 SQL 中实际占位符集合一致，避免发布出无法绑定的快照。"""
    errors: list[str] = []
    found = discover_placeholders(sql)
    missing_declared = [name for name in found if name not in declared]
    unused_declared = [name for name in declared if name not in found]
    if missing_declared:
        errors.append(
            "SQL 片段中的占位符未声明：" + "、".join(missing_declared)
        )
    if unused_declared:
        errors.append(
            "声明的占位符未在 SQL 片段中出现：" + "、".join(unused_declared)
        )
    return errors


def _split_default_pairs(raw: Any) -> tuple[list[tuple[Any, Any]], list[str]]:
    errors: list[str] = []
    if isinstance(raw, dict):
        return list(raw.items()), errors
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return [], errors
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return [], [f"参数默认值不是合法 JSON：{exc.msg}"]
            if isinstance(parsed, dict):
                return list(parsed.items()), errors
            if isinstance(parsed, list):
                raw = parsed
            else:
                return [], ["参数默认值 JSON 必须是对象或键值数组"]
        else:
            pairs: list[tuple[Any, Any]] = []
            for chunk in re.split(r"[,，;；\n]+", text):
                chunk = chunk.strip()
                if not chunk:
                    continue
                if "=" not in chunk:
                    errors.append(f"参数默认值「{chunk}」缺少等号")
                    continue
                key, value = chunk.split("=", 1)
                pairs.append((key.strip(), value.strip()))
            return pairs, errors
    if isinstance(raw, list):
        pairs = []
        for index, item in enumerate(raw, start=1):
            if isinstance(item, dict):
                key = (
                    item.get("名称")
                    or item.get("参数名")
                    or item.get("占位符")
                    or item.get("name")
                    or ""
                )
                if "值" in item:
                    value = item.get("值")
                elif "value" in item:
                    value = item.get("value")
                elif "默认值" in item:
                    value = item.get("默认值")
                else:
                    value = None
                pairs.append((key, value))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
            else:
                errors.append(f"第 {index} 个参数默认值必须是键值对象或二元数组")
        return pairs, errors
    return [], ["参数默认值必须是对象、JSON 对象字符串或键值数组"]


def normalize_defaults(raw: Any, placeholders: list[str]) -> tuple[dict[str, Any], list[str]]:
    """规范化参数默认值：键必须在已声明占位符里，重复键直接报错。"""
    pairs, errors = _split_default_pairs(raw)
    found: dict[str, Any] = {}
    positions: dict[str, int] = {}
    for index, (raw_key, raw_value) in enumerate(pairs, start=1):
        key = normalize_placeholder_name(raw_key)
        if not key:
            continue
        if key not in placeholders:
            errors.append(f"参数默认值「{key}」对应的占位符未声明")
            continue
        if key in positions:
            errors.append(
                f"参数默认值「{key}」重复声明（第 {positions[key]} 项与第 {index} 项）"
            )
            continue
        if isinstance(raw_value, (dict, list, tuple)):
            errors.append(f"参数默认值「{key}」必须是标量（字符串、数字或布尔值）")
            continue
        positions[key] = index
        found[key] = raw_value
    ordered = {name: found[name] for name in placeholders if name in found}
    return ordered, errors


def normalize_expected_columns(raw: Any) -> tuple[list[str], list[str]]:
    if raw is None or raw == "":
        return [], []
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return [], [f"预期输出列不是合法 JSON：{exc.msg}"]
            if not isinstance(parsed, list):
                return [], ["预期输出列 JSON 必须是数组"]
            raw = parsed
        else:
            raw = [part for part in _SPLIT_RE.split(text) if part]
    if not isinstance(raw, (list, tuple)):
        return [], ["预期输出列必须是数组、JSON 数组字符串或分隔字符串"]
    columns: list[str] = []
    for item in raw:
        column = str(item).strip()
        if column and column not in columns:
            columns.append(column)
    return columns, []


def build_core_snapshot(
    *,
    template_no: str,
    template_name: str,
    template_type: str,
    sql_fragment: str,
    placeholders: list[str],
    defaults: dict[str, Any],
    expected_columns: list[str],
    version_label: str,
    revision: str,
) -> dict[str, Any]:
    """不可变版本的核心内容；快照摘要只依赖这里的字段。"""
    return {
        "模板编号": str(template_no),
        "模板名称": str(template_name or ""),
        "模板类型": str(template_type or ""),
        "SQL片段": sql_fragment,
        "参数占位符": list(placeholders),
        "参数默认值": {key: defaults[key] for key in sorted(defaults)},
        "预期输出列": list(expected_columns),
        "模板版本": str(version_label),
        "修订号": str(revision or ""),
    }


def snapshot_digest(core: dict[str, Any]) -> str:
    return sha256_text(canonical_json(core))


def parameter_signature(placeholders: list[str], defaults: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            {
                "参数占位符": list(placeholders),
                "参数默认值": {key: defaults[key] for key in sorted(defaults)},
            }
        )
    )


def content_signature(
    *,
    template_no: str,
    sql_fragment: str,
    placeholders: list[str],
    defaults: dict[str, Any],
    expected_columns: list[str],
) -> str:
    """版本内容指纹：忽略版本标签与修订时间，用于上传/导入去重。"""
    return sha256_text(
        canonical_json(
            {
                "模板编号": str(template_no),
                "SQL片段": sql_fragment,
                "参数占位符": list(placeholders),
                "参数默认值": {key: defaults[key] for key in sorted(defaults)},
                "预期输出列": list(expected_columns),
            }
        )
    )


def diff_parameter_contract(
    previous_core: dict[str, Any] | None,
    current_core: dict[str, Any],
) -> list[str]:
    """对比新旧版本的参数契约，把可能改变作业 SQL 语义的差异列出来。"""
    if previous_core is None:
        return []
    previous_placeholders = list(previous_core.get("参数占位符") or [])
    current_placeholders = list(current_core.get("参数占位符") or [])
    previous_defaults = dict(previous_core.get("参数默认值") or {})
    current_defaults = dict(current_core.get("参数默认值") or {})
    conflicts: list[str] = []

    for name in previous_placeholders:
        if name not in current_placeholders:
            conflicts.append(f"占位符「{name}」在新版本中被移除")
    for name in current_placeholders:
        if name not in previous_placeholders:
            conflicts.append(f"占位符「{name}」为新增参数")
    for name in current_placeholders:
        if name not in previous_placeholders:
            continue
        old_value = previous_defaults.get(name, None)
        new_value = current_defaults.get(name, None)
        old_has = name in previous_defaults
        new_has = name in current_defaults
        if old_has != new_has or old_value != new_value:
            old_text = "未设置" if not old_has else str(old_value)
            new_text = "未设置" if not new_has else str(new_value)
            conflicts.append(
                f"占位符「{name}」默认值由「{old_text}」改为「{new_text}」"
            )
    if previous_core.get("SQL片段") != current_core.get("SQL片段"):
        conflicts.append("SQL片段已变更，新版本切换后新绑定按新语义执行")
    return conflicts


def render_sql(sql_fragment: str, values: dict[str, Any]) -> tuple[str, list[str]]:
    """用绑定值替换占位符；返回渲染结果和仍未解析的占位符。"""
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            if name not in unresolved:
                unresolved.append(name)
            return match.group(0)
        value = values[name]
        return "" if value is None else str(value)

    return PLACEHOLDER_RE.sub(replace, sql_fragment or ""), unresolved


def normalize_bound_parameters(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """规范化作业提交的参数，重复参数名只允许取值一致。"""
    if raw is None or raw == "":
        return {}, []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}, []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return {}, [f"绑定参数不是合法 JSON：{exc.msg}"]
        raw = parsed
    if isinstance(raw, dict):
        pairs: list[tuple[Any, Any]] = list(raw.items())
    elif isinstance(raw, list):
        pairs = []
        for index, item in enumerate(raw, start=1):
            if isinstance(item, dict):
                key = (
                    item.get("名称")
                    or item.get("参数名")
                    or item.get("占位符")
                    or item.get("name")
                    or ""
                )
                value = item.get("值", item.get("value"))
                pairs.append((key, value))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                pairs.append((item[0], item[1]))
            else:
                return {}, [f"第 {index} 个绑定参数必须是键值对象或二元数组"]
    else:
        return {}, ["绑定参数必须是对象、JSON 对象字符串或键值数组"]

    values: dict[str, Any] = {}
    for raw_key, raw_value in pairs:
        key = normalize_placeholder_name(raw_key)
        if not key:
            continue
        if isinstance(raw_value, (dict, list, tuple)):
            return {}, [f"参数「{key}」的绑定值必须是标量"]
        if key in values and values[key] != raw_value:
            return {}, [f"参数「{key}」重复绑定且取值不一致"]
        values[key] = raw_value
    return values, []


def safe_file_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return stem or "template"


def version_file_name(template_no: str, version_label: str) -> str:
    return f"{safe_file_stem(template_no)}-{safe_file_stem(version_label)}.sql"
