"""SQL模板版本发布与ETL作业绑定：端到端核对脚本。

用法：
    cd backend && .venv/bin/python scripts/verify_template_binding.py

脚本直接调用服务层与 FastAPI 应用（不依赖第三方测试库），覆盖：
1. 版本创建时的占位符/默认值校验与重复冲突拦截；
2. 并发发布同一模板只允许一个版本生效；
3. 重复绑定同一请求只生成一个结果；
4. 旧版本绑定不被新版本发布改写；
5. 文件下载/上传、版本包导入后 SQL 片段逐字保留；
6. 作业清单能把模板版本与参数绑定回查出来。
"""
from __future__ import annotations

import io
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def main() -> int:
    client = TestClient(app)

    print("\n[1] 示例数据：已发布版本与作业绑定可回查")
    templates = client.get("/api/sqltpl").json()["items"]
    first = next(item for item in templates if item["模板编号"] == "SQLT-0001")
    check("模板列表带当前发布版本", first.get("当前发布版本") == "v1", str(first))
    jobs = client.get("/api/etljob").json()["items"]
    bound = next(item for item in jobs if item["作业编号"] == "ETLJ-0003")
    check("作业清单回写模板版本", bound.get("模板版本") == "v1", str(bound))
    check("作业清单回写绑定状态", bound.get("绑定状态") == "已绑定", str(bound))
    detail = client.get(f"/api/etljob/{bound['id']}/binding").json()
    check("绑定明细含快照摘要", bool(detail["binding"].get("快照摘要")))
    check("绑定明细含参数绑定", detail["binding"].get("参数绑定") == {"dt": "2026-11-15", "region_id": 1101}, str(detail["binding"].get("参数绑定")))
    check("绑定明细含渲染SQL", "region_id = 1101" in detail["binding"].get("渲染SQL", ""), detail["binding"].get("渲染SQL", ""))

    print("\n[2] 版本校验：重复占位符、默认值不匹配、占位符缺失")
    temp = client.post("/api/sqltpl", json={"values": {"模板编号": "SQLT-TEST", "模板名称": "验证模板", "模板类型": "聚合"}}).json()
    template_id = temp["entry"]["id"]
    bad_dup = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={"values": {"模板版本": "v1", "SQL片段": "SELECT {{ dt }}, {{ dt }}", "参数占位符": ["dt", "dt"], "参数默认值": {"dt": "1"}}},
    )
    check("重复占位符被拦截", bad_dup.status_code == 400 and "重复声明" in bad_dup.json()["message"], bad_dup.text)
    bad_default = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={"values": {"模板版本": "v1", "SQL片段": "SELECT {{ dt }}", "参数占位符": ["dt"], "参数默认值": {"dt": "1", "dt2": "2"}}},
    )
    check("未声明占位符的默认值被拦截", bad_default.status_code == 400 and "未声明" in bad_default.json()["message"], bad_default.text)
    bad_missing = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={"values": {"模板版本": "v1", "SQL片段": "SELECT {{ dt }}, {{ region }}", "参数占位符": ["dt"]}},
    )
    check("SQL中未声明的占位符被拦截", bad_missing.status_code == 400 and "未声明" in bad_missing.json()["message"], bad_missing.text)

    print("\n[3] 并发发布：同一模板只允许一个版本生效")
    for label, default in (("v2", "2026-11-16"), ("v3", "2026-11-17")):
        created = client.post(
            f"/api/sqltpl/{template_id}/versions",
            json={
                "values": {
                    "模板版本": label,
                    "SQL片段": "SELECT count(*) FROM ods_order WHERE dt = '{{ dt }}'",
                    "参数占位符": ["dt"],
                    "参数默认值": {"dt": default},
                }
            },
        )
        check(f"草稿 {label} 创建成功", created.status_code == 200 and created.json()["ok"], created.text)
    version_ids = [row["id"] for row in client.get(f"/api/sqltpl/{template_id}/versions").json()["items"]]
    results: list[int] = []

    def publish(version_id: int) -> None:
        response = client.post(f"/api/sqltpl/{template_id}/versions/{version_id}/publish")
        results.append(response.status_code)

    threads = [threading.Thread(target=publish, args=(vid,)) for vid in version_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    active = [row for row in client.get(f"/api/sqltpl/{template_id}/versions").json()["items"] if row["status"] == "已发布"]
    check("并发发布后仅一个已发布版本", len(active) == 1, str(active))
    check("并发发布均返回成功或幂等成功", all(code == 200 for code in results), str(results))

    print("\n[4] 重复绑定幂等 + 旧版本继续可用")
    draft_job = client.post(
        "/api/etljob",
        json={"values": {"作业编号": "ETLJ-TEST", "作业名称": "验证作业", "源端数据表": "ods_order"}},
    ).json()
    job_id = draft_job["entry"]["id"]
    published = active[0]
    payload = {"values": {"模板ID": template_id, "模板版本ID": published["id"], "参数绑定": {}}}
    first_bind = client.post(f"/api/etljob/{job_id}/bind-template", json=payload)
    second_bind = client.post(f"/api/etljob/{job_id}/bind-template", json=payload)
    check("首次绑定成功", first_bind.status_code == 200 and first_bind.json()["ok"], first_bind.text)
    check("重复绑定返回同一绑定ID", first_bind.json()["entry"]["binding"]["id"] == second_bind.json()["entry"]["binding"]["id"], second_bind.text)
    check("重复绑定标记幂等", second_bind.json()["entry"]["idempotent"] is True, second_bind.text)
    bindings = client.get(f"/api/etljob/bindings?job_id={job_id}").json()
    check("绑定记录只有一条", bindings["total"] == 1, str(bindings["total"]))
    job_row = client.get(f"/api/etljob/{job_id}").json()
    check("作业清单回写渲染SQL", "count(*)" in job_row.get("渲染SQL", ""), job_row.get("渲染SQL", ""))

    superseded_version = next(
        row for row in client.get(f"/api/sqltpl/{template_id}/versions").json()["items"] if row["status"] == "已替代"
    )
    legacy = client.post(
        f"/api/etljob/{job_id}/bind-template",
        json={"values": {"模板ID": template_id, "模板版本ID": superseded_version["id"]}},
    )
    check("已替代版本仍可被作业绑定", legacy.status_code == 200 and legacy.json()["ok"], legacy.text)
    check("历史绑定与原作业共享旧版本", legacy.json()["entry"]["binding"]["版本是否最新"] is False, legacy.text)

    draft_version = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={
            "values": {
                "模板版本": "v9-draft",
                "SQL片段": "SELECT 1 WHERE dt = '{{ dt }}'",
                "参数占位符": ["dt"],
                "参数默认值": {"dt": "2026-11-20"},
            }
        },
    ).json()["entry"]["version"]
    not_published = client.post(
        f"/api/etljob/{job_id}/bind-template",
        json={"values": {"模板ID": template_id, "模板版本ID": draft_version["id"]}},
    )
    check("草稿版本不可绑定", not_published.status_code == 409, not_published.text)

    print("\n[5] 文件下载/上传与版本包导入")
    file_response = client.get(f"/api/sqltpl/{template_id}/versions/{published['id']}/file")
    check("版本文件可下载", file_response.status_code == 200 and b"count(*)" in file_response.content, file_response.text[:120])
    upload = client.post(
        f"/api/sqltpl/{template_id}/versions/upload",
        files={"file": ("new.sql", "SELECT count(*) FROM ods_order WHERE dt = '{{ dt }}' AND region = {{ region }}".encode())},
        data={"模板版本": "v4", "参数占位符": "dt, region", "参数默认值": "dt=2026-11-18, region=7"},
    )
    check("上传新文件生成草稿版本", upload.status_code == 200 and upload.json()["created"] is True, upload.text)
    duplicate_upload = client.post(
        f"/api/sqltpl/{template_id}/versions/upload",
        files={"file": ("new.sql", "SELECT count(*) FROM ods_order WHERE dt = '{{ dt }}' AND region = {{ region }}".encode())},
        data={"模板版本": "v5", "参数占位符": "dt, region", "参数默认值": "dt=2026-11-18, region=7"},
    )
    check("上传同一内容不生成重复版本", duplicate_upload.status_code == 200 and duplicate_upload.json()["idempotent"] is True, duplicate_upload.text)

    bundle = client.get("/api/sqltpl/export/bundle")
    check("版本包可导出", bundle.status_code == 200 and bundle.headers["content-type"].startswith("application/json"), bundle.headers.get("content-type", ""))
    payload = json.loads(bundle.content)
    imported = client.post("/api/sqltpl/import", json=payload)
    check("重复导入全部复用不新建", imported.status_code == 200 and imported.json()["imported_count"] == 0 and imported.json()["reused_count"] == payload["total"], imported.text)
    fresh_template = client.post(
        "/api/sqltpl", json={"values": {"模板编号": "SQLT-IMP", "模板名称": "导入模板", "模板类型": "清洗"}}
    ).json()["entry"]
    sql_text = "SELECT a, b\nFROM t\nWHERE dt = '{{ dt }}'   -- 保留注释与空白"
    single = {
        "format": "etl-sqltpl-version",
        "versions": [
            {
                "模板编号": "SQLT-IMP",
                "模板版本": "v1",
                "status": "已发布",
                "快照": {
                    "模板编号": "SQLT-IMP",
                    "模板名称": "导入模板",
                    "模板类型": "清洗",
                    "模板版本": "v1",
                    "修订号": "2026-11-16T00:00:00+00:00",
                    "SQL片段": sql_text,
                    "参数占位符": ["dt"],
                    "参数默认值": {"dt": "2026-11-16"},
                    "预期输出列": ["a", "b"],
                },
            }
        ],
    }
    imported_new = client.post(
        "/api/sqltpl/import",
        files={"file": ("bundle.json", json.dumps(single, ensure_ascii=False).encode(), "application/json")},
    )
    check("导入新模板版本成功", imported_new.status_code == 200 and imported_new.json()["imported_count"] == 1, imported_new.text)
    imported_versions = client.get(f"/api/sqltpl/{fresh_template['id']}/versions").json()["items"]
    check("导入后 SQL 片段逐字保留", imported_versions and imported_versions[0]["SQL片段"] == sql_text, str(imported_versions[:1]))

    print("\n[6] 对侧作业沿用同一版本")
    counterpart = client.post(
        "/api/etljob",
        json={"values": {"作业编号": "ETLJ-TEST-2", "作业名称": "对侧作业", "源端数据表": "dwd_order"}},
    ).json()["entry"]
    client.post(
        f"/api/etljob/{counterpart['id']}/bind-template",
        json={"values": {"模板ID": template_id, "模板版本ID": published["id"]}},
    )
    linked = client.post(
        f"/api/etljob/{job_id}/bind-template",
        json={
            "values": {
                "模板ID": template_id,
                "模板版本ID": published["id"],
                "参数绑定": {},
                "对侧作业编号": "ETLJ-TEST-2",
            }
        },
    )
    # 该请求与已有绑定同版本同参数，应复用绑定并补齐对侧关联
    check("对侧关联复用绑定", linked.status_code == 200 and linked.json()["entry"]["idempotent"] is True, linked.text)
    counterpart_row = client.get(f"/api/etljob/{counterpart['id']}").json()
    check("对侧作业沿用同一版本", counterpart_row.get("模板版本ID") == published["id"], str(counterpart_row.get("模板版本ID")))
    check("对侧作业标记版本一致", counterpart_row.get("对侧版本一致") is True, str(counterpart_row))

    print("\n[7] 作业清单内部表不进概览")
    overview = client.get("/api/overview").json()
    names = [row["name"] for row in overview["modules"]]
    check("内部表未出现在概览", "sqltpl_version" not in names and "etljob_binding" not in names, str(names))

    print("\n[8] 模板状态受版本发布约束")
    direct_publish = client.post(f"/api/sqltpl/{template_id}/actions", json={"values": {"action": "发布模板"}})
    check("未选版本不能直接发布模板", direct_publish.json()["ok"] is False and "版本" in direct_publish.json()["message"], direct_publish.text)
    impact = client.get(f"/api/sqltpl/{template_id}/versions/{published['id']}/impact").json()
    check("影响面返回旧版本绑定作业数", isinstance(impact.get("旧版本绑定作业数"), int), str(impact)[:200])
    check("旧版本绑定不被新发布改写", impact["旧版本绑定作业数"] >= 0, str(impact)[:200])
    legacy_detail = client.get(f"/api/etljob/{job_id}/binding").json()
    check("作业仍指向自己绑定的版本", legacy_detail["binding"]["版本ID"] == superseded_version["id"], str(legacy_detail["binding"]["版本ID"]))
    republish_old = client.post(f"/api/sqltpl/{template_id}/versions/{superseded_version['id']}/publish")
    check("已替代版本不能重新发布", republish_old.status_code == 409, republish_old.text)
    version_detail = client.get(f"/api/sqltpl/{template_id}/versions/{published['id']}").json()
    check("版本文档含完整不可变快照", version_detail.get("快照", {}).get("快照摘要") is None and version_detail.get("快照", {}).get("SQL片段"), str(version_detail.get("快照"))[:120])

    print("\n[9] 参数校验：未知参数与缺省值")
    unknown_param = client.post(
        f"/api/etljob/{job_id}/bind-template",
        json={"values": {"模板ID": template_id, "模板版本ID": published["id"], "参数绑定": {"不存在的参数": "x"}}},
    )
    check("未声明参数被拒绝", unknown_param.status_code == 400 and "未声明" in unknown_param.json()["message"], unknown_param.text)
    no_default_version = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={
            "values": {
                "模板版本": "v10-nodefault",
                "SQL片段": "SELECT 1 WHERE dt = '{{ dt }}'",
                "参数占位符": ["dt"],
                "参数默认值": {},
            }
        },
    ).json()["entry"]["version"]
    check("显式空默认值不继承基线", no_default_version["参数默认值"] == {}, str(no_default_version["参数默认值"]))
    active_before_inherit = next(
        row for row in client.get(f"/api/sqltpl/{template_id}/versions").json()["items"] if row["status"] == "已发布"
    )
    inherited_version = client.post(
        f"/api/sqltpl/{template_id}/versions",
        json={
            "values": {
                "模板版本": "v11-inherit",
                "SQL片段": "SELECT 2 WHERE dt = '{{ dt }}'",
                "参数占位符": ["dt"],
            }
        },
    ).json()["entry"]["version"]
    check(
        "未提交默认值时继承当前发布版本",
        inherited_version["参数默认值"] == active_before_inherit["参数默认值"],
        f"{inherited_version['参数默认值']} != {active_before_inherit['参数默认值']}",
    )
    client.post(f"/api/sqltpl/{template_id}/versions/{no_default_version['id']}/publish")
    missing_param = client.post(
        f"/api/etljob/{job_id}/bind-template",
        json={"values": {"模板ID": template_id, "模板版本ID": no_default_version["id"]}},
    )
    check("缺参数且无默认值被拒绝", missing_param.status_code == 400 and "缺少占位符" in missing_param.json()["message"], missing_param.text)

    print("\n[10] 并发重复绑定只生成一个结果")
    from app.services.etljob import EtljobService
    from app.template_repo import active_version, bindings_of_job

    concurrent_job = client.post(
        "/api/etljob",
        json={"values": {"作业编号": "ETLJ-CONC", "作业名称": "并发绑定", "源端数据表": "ods_order"}},
    ).json()["entry"]
    template_row = store.find("sqltpl", template_id)
    current = active_version(template_id)
    binding_service = EtljobService()
    bound_ids: list[int] = []
    errors: list[str] = []

    def bind_once() -> None:
        try:
            result = binding_service.bind_template_version(
                int(concurrent_job["id"]),
                {
                    "模板ID": template_id,
                    "模板版本ID": current["id"],
                    "参数绑定": {"dt": "2026-11-19"},
                },
            )
            bound_ids.append(int(result["binding"]["id"]))
        except Exception as exc:  # noqa: BLE001 - 并发测试需要收集所有异常
            errors.append(str(exc))

    workers = [threading.Thread(target=bind_once) for _ in range(12)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    check("并发绑定无异常", not errors, str(errors[:3]))
    check("并发绑定只产生一个绑定ID", len(set(bound_ids)) == 1, str(set(bound_ids)))
    check("并发绑定只写入一条绑定记录", len(bindings_of_job(int(concurrent_job["id"]))) == 1, str(len(bindings_of_job(int(concurrent_job["id"])))))

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部核对项通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
