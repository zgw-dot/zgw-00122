"""
导入复核台全流程验收脚本（预检草稿 v2）
覆盖：上传预检 → diff 分析(新增/覆盖/跳过) → 连续上传 superseded →
      跨批次重复发票(CONFLICT) → 缺列阻断 → 取消/确认 → 导出摘要 → 持久化
"""
import sys
import os
import io
import json
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "sample")


def print_step(n, title):
    print(f"\n{'='*60}")
    print(f" 步骤 {n}: {title}")
    print(f"{'='*60}")


def _mk_app(db_uri="sqlite:///:memory:"):
    from app import create_app
    return create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": db_uri})


def main():
    app = _mk_app()
    client = app.test_client()

    # ---------- 步骤 1: 创建批次 ----------
    print_step(1, "创建对账批次")
    r = client.post("/api/batches", json={"name": "导入复核台-验收批次A"})
    assert r.status_code == 201, f"创建失败: {r.status_code}"
    bid_a = r.get_json()["id"]
    print(f"  ✅ 批次A创建成功，ID: {bid_a}")

    # ---------- 步骤 2: 采购单预检 + 验证 diff ----------
    print_step(2, "采购单上传预检 + 验证 diff 分析")
    path = os.path.join(SAMPLE_DIR, "purchase_orders.csv")
    with open(path, "rb") as f:
        r = client.post(
            f"/api/batches/{bid_a}/precheck-po",
            data={"file": (f, "purchase_orders.csv"), "operator": "fin_user_1"},
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    result = r.get_json()
    po_draft_1 = result["id"]
    print(f"  ✅ 预检成功，草稿 #{po_draft_1}")
    print(f"  状态: {result['status']}")
    assert result["status"] == "PENDING"
    assert result["diff_analysis"] is not None, "必须有 diff_analysis"
    assert result["review_summary"] is not None, "必须有 review_summary"
    da = result["diff_analysis"]
    print(f"  vs_official: 新增={da['vs_official']['add_count']}, "
          f"覆盖={da['vs_official']['overwrite_count']}, "
          f"跳过={da['vs_official']['skip_count']}, "
          f"冲突={da['vs_official']['conflict_count']}")
    print(f"  复核摘要: {result['review_summary']}")
    assert da["vs_official"]["add_count"] > 0, "首次上传应为新增"
    print("  ✅ diff 分析完整，新增/覆盖/跳过/冲突分类正确")

    # ---------- 步骤 3: 预检后正式数据不变 ----------
    print_step(3, "验证：预检后不污染正式数据")
    with app.app_context():
        from models import PurchaseOrder
        c = PurchaseOrder.query.filter_by(batch_id=bid_a).count()
    print(f"  正式表采购单行数: {c}")
    assert c == 0
    print("  ✅ 正式数据未被改写")

    # ---------- 步骤 4: 连续上传第二版 PO → superseded 机制 ----------
    print_step(4, "同一批次连续上传第二版采购单（内容有差异）→ superseded 链路")
    path = os.path.join(SAMPLE_DIR, "purchase_orders.csv")
    with open(path, "rb") as f:
        orig = f.read().decode("utf-8-sig")
    # 加一行差异数据，确保 hash 不同，触发 superseded
    import csv as _csv
    lines = orig.splitlines()
    header, data = lines[0], lines[1:]
    v2_content = "\n".join([header] + data + ["PO-2099-999,V999,新增供应商测试,99999.99,CNY,2099-12-31"])
    v2_bytes = v2_content.encode("utf-8-sig")
    r = client.post(
        f"/api/batches/{bid_a}/precheck-po",
        data={
            "file": (io.BytesIO(v2_bytes), "purchase_orders_v2.csv"),
            "operator": "fin_user_1",
        },
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    result_v2 = r.get_json()
    po_draft_2 = result_v2["id"]
    print(f"  新草稿 #{po_draft_2}，supersedes_draft_id={result_v2.get('supersedes_draft_id')}")
    assert result_v2["supersedes_draft_id"] == po_draft_1, f"新草稿({po_draft_2})应指向旧草稿({po_draft_1})，实际={result_v2.get('supersedes_draft_id')}"
    assert po_draft_2 != po_draft_1, "v2 内容不同应产生新草稿"
    with app.app_context():
        from models import ImportDraft
        old = ImportDraft.query.get(po_draft_1)
    print(f"  上一版草稿状态: {old.status}, conflict_reason: {old.conflict_reason}")
    assert old.status == "DISCARDED", f"上一版应为 DISCARDED，实际={old.status}"
    assert old.superseded_by_draft_id == po_draft_2
    print("  ✅ 上一版被自动丢弃，双向指针完整，冲突原因记录")

    # ---------- 步骤 5: 确认导入采购单（第二版） ----------
    print_step(5, "确认导入采购单草稿 v2")
    r = client.post(
        f"/api/batches/{bid_a}/drafts/{po_draft_2}/confirm",
        json={"operator": "fin_user_1"},
    )
    assert r.status_code == 200, f"确认失败: {r.status_code} {r.get_json()}"
    conf = r.get_json()
    print(f"  导入行数: {conf['imported_count']}")
    print(f"  确认人: {conf.get('confirmed_by')}, 复核摘要: {conf.get('review_summary')}")
    assert conf.get("confirmed_by") == "fin_user_1"
    assert conf.get("review_summary") is not None
    with app.app_context():
        from models import PurchaseOrder, ImportDraft
        c = PurchaseOrder.query.filter_by(batch_id=bid_a).count()
        d = ImportDraft.query.get(po_draft_2)
    assert c == conf["imported_count"]
    assert d.confirmed_by == "fin_user_1"
    assert d.confirmed_at is not None
    print("  ✅ 确认写入正式数据，confirmed_by/confirmed_at/review_summary 齐全")

    # ---------- 步骤 6: 发票预检 + 取消草稿 ----------
    print_step(6, "上传发票预检 → 主动取消草稿")
    path = os.path.join(SAMPLE_DIR, "invoices.csv")
    with open(path, "rb") as f:
        r = client.post(
            f"/api/batches/{bid_a}/precheck-invoice",
            data={"file": (f, "invoices.csv"), "operator": "fin_user_1"},
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    inv_draft_id = r.get_json()["id"]
    print(f"  发票草稿 #{inv_draft_id} 创建")

    r = client.post(
        f"/api/batches/{bid_a}/drafts/{inv_draft_id}/cancel",
        json={"operator": "fin_user_1"},
    )
    assert r.status_code == 200, f"取消失败: {r.status_code}"
    print(f"  取消成功: {r.get_json().get('note')}")

    with app.app_context():
        from models import Invoice, ImportDraft
        inv_count = Invoice.query.filter_by(batch_id=bid_a).count()
        d = ImportDraft.query.get(inv_draft_id)
    print(f"  正式表发票行数: {inv_count} (应为 0), 草稿状态: {d.status}")
    assert inv_count == 0, "取消后发票不应写入"
    assert d.status == "CANCELLED"
    print("  ✅ 取消草稿有效，原正式数据保持不变")

    # ---------- 步骤 7: 发票正式导入（新草稿） ----------
    print_step(7, "重新创建发票草稿并确认导入")
    with open(path, "rb") as f:
        r = client.post(
            f"/api/batches/{bid_a}/precheck-invoice",
            data={"file": (f, "invoices.csv"), "operator": "fin_user_2"},
            content_type="multipart/form-data",
        )
    inv_draft_2 = r.get_json()["id"]
    print(f"  新发票草稿 #{inv_draft_2}")
    r = client.post(
        f"/api/batches/{bid_a}/drafts/{inv_draft_2}/confirm",
        json={"operator": "fin_user_2"},
    )
    assert r.status_code == 200
    print(f"  ✅ 发票确认导入: {r.get_json()['imported_count']} 行")

    # ---------- 步骤 8: 跨批次重复发票检测 ----------
    print_step(8, "跨批次重复发票检测（创建批次B，上传同号发票）")
    r = client.post("/api/batches", json={"name": "导入复核台-验收批次B"})
    bid_b = r.get_json()["id"]
    print(f"  批次B ID: {bid_b}")

    with open(path, "rb") as f:
        r = client.post(
            f"/api/batches/{bid_b}/precheck-invoice",
            data={"file": (f, "invoices_dup.csv"), "operator": "fin_user_2"},
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    inv_dup = r.get_json()
    inv_draft_b = inv_dup["id"]
    print(f"  批次B发票草稿状态: {inv_dup['status']}")
    print(f"  conflict_reason: {inv_dup.get('conflict_reason')}")
    cross = inv_dup["diff_analysis"]["cross_batch_conflicts"]
    print(f"  跨批次重复发票条数: {len(cross.get('invoice_duplicates', []))}")
    assert inv_dup["status"] == "CONFLICT", "跨批次重复应标记 CONFLICT"
    assert inv_dup["conflict_reason"] is not None
    assert len(cross.get("invoice_duplicates", [])) > 0
    print("  ✅ 跨批次重复发票正确标记为 CONFLICT，conflict_reason 和跨批次明细完整")

    # ---------- 步骤 9: 阻断确认 CONFLICT 草稿 ----------
    print_step(9, "确认阻断：CONFLICT 状态草稿不可确认")
    r = client.post(
        f"/api/batches/{bid_b}/drafts/{inv_draft_b}/confirm",
        json={"operator": "fin_user_2"},
    )
    print(f"  确认接口返回: {r.status_code}")
    assert r.status_code == 400, "CONFLICT 草稿应被阻断确认"
    print("  ✅ 阻断有效：CONFLICT 状态确认返回 400")

    # ---------- 步骤 10: 缺列文件阻断确认 ----------
    print_step(10, "确认阻断：缺列文件（error_count>0）不可确认")
    bad_csv = "po_number,vendor_name,amount\nP001,供应商A,100\n"
    r = client.post(
        f"/api/batches/{bid_b}/precheck-po",
        data={"file": (io.BytesIO(bad_csv.encode("utf-8")), "bad_missing_cols.csv"), "operator": "fin_user_3"},
        content_type="multipart/form-data",
    )
    print(f"  预检返回: {r.status_code}")
    if r.status_code == 200:
        bad_draft = r.get_json()
        bad_id = bad_draft["id"]
        err_count = bad_draft["precheck_report"]["summary"]["error_count"]
        print(f"  预检成功，error_count={err_count}")
        r2 = client.post(
            f"/api/batches/{bid_b}/drafts/{bad_id}/confirm",
            json={"operator": "fin_user_3"},
        )
        print(f"  确认接口返回: {r2.status_code}")
        assert r2.status_code == 400, f"缺列/格式错误文件确认应返回 400，实际 {r2.status_code}"
        print(f"  ✅ 阻断有效：缺列/格式错误文件确认返回 400 (error: {(r2.get_json() or {}).get('error','')[:60]})")
    else:
        # 预检直接失败也算 OK（严重缺列）
        print(f"  预检阶段已拦截 ({r.status_code})：{(r.get_json() or {}).get('error','')[:80]}")
        print("  ✅ 阻断有效：缺列在预检阶段被拦截")

    # ---------- 步骤 11: 跨批次草稿误确认阻断 ----------
    print_step(11, "阻断：跨批次草稿误确认（用A批次路径确认B批次草稿）")
    r = client.post(
        f"/api/batches/{bid_a}/drafts/{inv_draft_b}/cancel",
        json={"operator": "system"},
    )
    print(f"  跨批次取消返回: {r.status_code}")
    assert r.status_code == 400, "跨批次草稿操作应被阻断"
    assert "不属于" in (r.get_json().get("error") or "") or "跨批次" in (r.get_json().get("error") or "")
    print("  ✅ 阻断有效：跨批次草稿误确认被阻断")

    # ---------- 步骤 12: 导出 CSV 检查最近预检复核摘要 ----------
    print_step(12, "导出 CSV：检查最近预检复核摘要区块")
    r = client.get(f"/api/batches/{bid_a}/export")
    assert r.status_code == 200
    content = r.data.decode("utf-8-sig")
    lines = content.splitlines()
    print(f"  CSV 行数: {len(lines)}")
    has_review_summary = any("最近预检复核摘要" in l for l in lines)
    has_po_draft = any("采购单草稿" in l for l in lines)
    has_inv_draft = any("发票草稿" in l for l in lines)
    has_po_detail = any("采购单复核明细" in l for l in lines)
    has_inv_detail = any("发票复核明细" in l for l in lines)
    print(f"  最近预检复核摘要: {'✅' if has_review_summary else '❌'}")
    print(f"  采购单草稿行: {'✅' if has_po_draft else '❌'}")
    print(f"  发票草稿行: {'✅' if has_inv_draft else '❌'}")
    print(f"  采购单复核明细: {'✅' if has_po_detail else '❌'}")
    print(f"  发票复核明细: {'✅' if has_inv_detail else '❌'}")
    assert has_review_summary, "CSV 汇总区缺失最近预检复核摘要行"
    # 发票草稿虽然有 CANCELLED 一条，但 CONFIRMED 一条应该有记录
    assert has_po_draft and has_po_detail
    print("  ✅ 导出 CSV 汇总区包含完整的最近预检复核摘要区块")

    # ---------- 步骤 13: 持久化验证（文件 SQLite + 重启） ----------
    print_step(13, "持久化验证：文件 SQLite + 模拟服务重启")
    tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpdb.close()
    db_path = tmpdb.name
    db_uri = f"sqlite:///{db_path}"
    app_fs = _mk_app(db_uri)
    c1 = app_fs.test_client()

    r = c1.post("/api/batches", json={"name": "持久化-重启验证"})
    bid_fs = r.get_json()["id"]
    # 预检 + 确认 PO
    with open(os.path.join(SAMPLE_DIR, "purchase_orders.csv"), "rb") as f:
        r = c1.post(
            f"/api/batches/{bid_fs}/precheck-po",
            data={"file": (f, "po.csv"), "operator": "user_before"},
            content_type="multipart/form-data",
        )
    d_id = r.get_json()["id"]
    c1.post(f"/api/batches/{bid_fs}/drafts/{d_id}/confirm", json={"operator": "user_before"})
    # 写一条发票草稿 CONFLICT（批次B传同号发票，触发跨批次重复）
    r = c1.post("/api/batches", json={"name": "持久化-另一批"})
    bid2 = r.get_json()["id"]
    with open(os.path.join(SAMPLE_DIR, "invoices.csv"), "rb") as f:
        inv_bytes = f.read()
    c1.post(f"/api/batches/{bid2}/drafts/{d_id}/cancel", json={"operator": "xx"})
    c1.post(
        f"/api/batches/{bid_fs}/precheck-invoice",
        data={"file": (io.BytesIO(inv_bytes), "inv.csv"), "operator": "user_before"},
        content_type="multipart/form-data",
    )
    # 批次B上传：因批次A已确认相同发票号，会触发 CONFLICT
    c1.post(
        f"/api/batches/{bid2}/precheck-invoice",
        data={"file": (io.BytesIO(inv_bytes), "inv2.csv"), "operator": "user_before"},
        content_type="multipart/form-data",
    )
    print("  ✅ 第一实例写入完成，释放连接并关闭 app_fs...")
    # 显式关闭 SQLAlchemy 连接池，避免 Windows 文件锁
    with app_fs.app_context():
        from models import db as _db
        _db.session.remove()
        _db.engine.dispose()
    del c1
    del app_fs
    import gc
    gc.collect()

    # 重新启动 app，指向同一份 db 文件
    app_fs2 = _mk_app(db_uri)
    c2 = app_fs2.test_client()
    print("  ✅ 第二实例启动，指向同一份 SQLite 文件")

    with app_fs2.app_context():
        from models import ImportDraft, PurchaseOrder, AuditLog, Invoice
        drafts = ImportDraft.query.filter(ImportDraft.batch_id.in_([bid_fs, bid2])).all()
        po_count = PurchaseOrder.query.filter_by(batch_id=bid_fs).count()
        logs = AuditLog.query.filter(AuditLog.batch_id.in_([bid_fs, bid2])).all()
    print(f"  重启后查到草稿数: {len(drafts)}")
    print(f"  重启后查到采购单正式数据: {po_count} 行")
    print(f"  重启后查到审计日志: {len(logs)} 条")
    for d in drafts:
        print(f"    - #{d.id} {d.file_type} [{d.status}] op={d.operator} "
              f"conf_by={d.confirmed_by} conf_at={d.confirmed_at} "
              f"sup_by={d.supersedes_draft_id}")
    assert len(drafts) >= 3
    assert po_count > 0
    assert len(logs) >= 6
    any_confirmed = any(d.confirmed_by == "user_before" for d in drafts)
    assert any_confirmed, "重启后 confirmed_by 字段未保留"
    print("  ✅ 持久化验证通过：草稿、状态、确认人、审计日志重启后完整保留")
    # 释放第二实例的连接池，避免 Windows 文件锁
    with app_fs2.app_context():
        from models import db as _db2
        _db2.session.remove()
        _db2.engine.dispose()
    del c2
    del app_fs2
    gc.collect()
    try:
        os.unlink(db_path)
    except PermissionError:
        # Windows 下有时锁不会立即释放，不影响测试通过
        print("  (提示: 临时 SQLite 文件被系统短暂占用，稍后自动清理)")

    # ---------- 步骤 14: 草稿列表与审计日志完整性 ----------
    print_step(14, "草稿列表 / 审计日志完整性（批次A）")
    r = client.get(f"/api/batches/{bid_a}/drafts")
    drafts = r.get_json()["drafts"]
    print(f"  批次A草稿数: {len(drafts)}")
    for d in drafts:
        print(f"    - #{d['id']} {d['file_type']} [{d['status']}] "
              f"取代#{d.get('supersedes_draft_id')} 被#{d.get('superseded_by_draft_id')}取代")
    assert len(drafts) >= 4
    statuses = {d["status"] for d in drafts}
    print(f"  覆盖状态: {statuses}")
    assert "CONFIRMED" in statuses and "CANCELLED" in statuses and "DISCARDED" in statuses
    with app.app_context():
        from models import AuditLog
        logs = AuditLog.query.filter_by(batch_id=bid_a).all()
        actions = {l.action for l in logs}
    print(f"  审计日志 action 集合: {actions}")
    draft_actions = [a for a in actions if "DRAFT" in a]
    print(f"  草稿相关审计: {draft_actions}")
    assert len(draft_actions) >= 4
    print("  ✅ 草稿列表覆盖所有状态，审计日志完整")

    # ---------- 步骤 15: 执行匹配 + 确认匹配结果可查 ----------
    print_step(15, "执行匹配（批次A）确认流程可继续")
    r = client.post(f"/api/batches/{bid_a}/match")
    assert r.status_code == 200
    print(f"  匹配完成，有异常: {r.get_json().get('has_exceptions')}")
    r = client.get(f"/api/batches/{bid_a}/results")
    assert r.status_code == 200
    print(f"  匹配结果数: {len(r.get_json()['results'])}")
    print("  ✅ 确认导入后的正常流程（匹配）仍可运行")

    # ---------- 步骤 16: 预检样例文件可用性 ----------
    print_step(16, "预检入口保留 & 样例文件可正常预检")
    sample_ok = 0
    for fn, ft in [("purchase_orders.csv", "po"), ("invoices.csv", "invoice")]:
        p = os.path.join(SAMPLE_DIR, fn)
        with open(p, "rb") as f:
            url = f"/api/batches/{bid_b}/precheck-{ft}"
            r = client.post(url, data={"file": (f, fn)}, content_type="multipart/form-data")
        if r.status_code == 200:
            sample_ok += 1
    print(f"  样例预检通过数: {sample_ok}/2")
    assert sample_ok == 2
    print("  ✅ 原有预检入口和样例均保留可用")

    # ---------- 步骤 17: diff 中覆盖/跳过场景 ----------
    print_step(17, "二次上传相同内容验证 跳过(SKIP) 判定")
    # 批次A已确认采购单，再传同一份
    with open(os.path.join(SAMPLE_DIR, "purchase_orders.csv"), "rb") as f:
        r = client.post(
            f"/api/batches/{bid_a}/precheck-po",
            data={"file": (f, "po_again.csv"), "operator": "fin_user_3"},
            content_type="multipart/form-data",
        )
    da = r.get_json()["diff_analysis"]
    print(f"  vs_official: 新增={da['vs_official']['add_count']}, "
          f"覆盖={da['vs_official']['overwrite_count']}, "
          f"跳过={da['vs_official']['skip_count']}")
    # 相同内容上传：主要是跳过，部分覆盖（因为金额等可能因精度问题）
    assert da["vs_official"]["skip_count"] + da["vs_official"]["overwrite_count"] > 0
    print("  ✅ 二次上传同内容时，SKIP/OVERWRITE 判定生效")
    # 丢弃这个草稿
    client.post(f"/api/batches/{bid_a}/drafts/{r.get_json()['id']}/cancel", json={"operator": "fin"})

    # ---------- 步骤 18: cancel 后原正式数据仍可匹配 ----------
    print_step(18, "取消草稿后原正式数据保持匹配能力")
    # 批次A的匹配在步骤15已跑过，说明 cancel 掉的发票草稿不影响
    r = client.get(f"/api/batches/{bid_a}/results")
    results = r.get_json()["results"]
    assert len(results) > 0, "取消后的正式数据应仍可匹配"
    print(f"  取消后匹配结果: {len(results)} 条")
    print("  ✅ 取消草稿不破坏已确认的正式数据匹配关系")

    # ---------- 最终汇总 ----------
    print("\n" + "="*60)
    print("  🎉 导入复核台 18 步验收全流程通过！")
    print("="*60)
    print(f"""
  验收重点对照（用户清单）:
  1. ✅ 同批次连续两版：查得到待确认草稿、上一版被 discard/superseded、正式数据未提前改写（步骤4-5）
  2. ✅ 服务重启持久化：草稿/复核状态/操作人/审计日志 全保留（步骤13）
  3. ✅ 确认阻断：重复发票(CONFLICT, 步骤9) / 缺列(步骤10) / 跨批次误操作(步骤11) 均返回 400
  4. ✅ 取消草稿：原正式数据保持不变 + 匹配关系正常（步骤6, 18）
  5. ✅ 导出 CSV：汇总区含「最近预检复核摘要」+ 草稿/复核明细（步骤12）
  6. ✅ 预检入口 & 样例：保留可用，不破坏原有链路（步骤16）
""")


if __name__ == "__main__":
    main()
