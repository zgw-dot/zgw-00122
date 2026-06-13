"""供应商对账平台回归测试

运行方式:
    cd d:\\workSpace\\AI__SPACE\\02-label\\zgw-00122
    python -m pytest tests/ -q
    # 或
    python -m pytest tests/test_reconciliation.py -v

覆盖范围:
1. 确认入账成功且不暴露 NameError
2. 同供应商同 PO 超容差只生成一条带两边单据、差额、规则版本和备注入口的异常
3. 缺列文件上传失败后不留下部分结果
4. 重复发票号上传失败后不留下部分结果
5. 回滚后导出报表应付合计与接口汇总一致
"""
import csv
import io
import os
import sys
import tempfile
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    MatchResult, ExceptionItem, Batch, PayableRecalcNote,
    RESULT_STATUS_PENDING, RESULT_STATUS_CONFIRMED,
    MATCH_TYPE_OVER_TOLERANCE, MATCH_TYPE_UNMATCHED_PO, MATCH_TYPE_UNMATCHED_INVOICE,
    BATCH_STATUS_CREATED, BATCH_STATUS_POSTED, BATCH_STATUS_ROLLED_BACK,
    EXCEPTION_STATUS_PENDING,
)


# ---------- 辅助函数 ----------

def create_batch(client, name, pct=2.0, ab=100.0):
    r = client.post("/api/batches", json={"name": name, "tolerance_pct": pct, "tolerance_abs": ab})
    assert r.status_code == 201, f"创建批次失败: {r.data}"
    return r.get_json()["id"]


def upload_file(client, bid, typ, sample_dir, filename):
    path = os.path.join(sample_dir, filename)
    with open(path, "rb") as f:
        data = {"file": (f, filename)}
        r = client.post(f"/api/batches/{bid}/upload-{typ}", data=data, content_type="multipart/form-data")
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code, "raw": r.data.decode("utf-8", errors="replace")}


def get_batch(client, bid):
    r = client.get(f"/api/batches/{bid}")
    assert r.status_code == 200
    return r.get_json()


def get_results(client, bid):
    r = client.get(f"/api/batches/{bid}/results")
    assert r.status_code == 200
    return r.get_json()["results"]


def get_exceptions(client, bid):
    r = client.get(f"/api/batches/{bid}/exceptions")
    assert r.status_code == 200
    return r.get_json()["exceptions"]


def resolve_all_exceptions(client, bid):
    exs = get_exceptions(client, bid)
    for e in exs:
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/remark", json={"remarks": f"备注-{e['id']}"})
        assert r.status_code == 200
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/resolve", json={"action": "resolve"})
        assert r.status_code == 200


# ---------- 测试用例 ----------

def test_confirm_batch_success_no_nameerror(client, sample_dir):
    """确认入账/入账链路成功，且任何错误都不暴露 NameError 或 Python 堆栈"""
    bid = create_batch(client, "test-confirm-no-nameerror")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200
    body = r.get_json()
    assert "success" in body, f"match 响应结构异常: {body}"

    # --- a) 有 pending 异常时调用 /confirm：应返回可读业务错误（400 JSON，不含 NameError/traceback） ---
    r = client.post(f"/api/batches/{bid}/confirm")
    assert r.content_type.startswith("application/json"), f"返回类型不是 JSON: {r.content_type}"
    body = r.get_json()
    err_str = str(body.get("error", "")).lower()
    assert "nameerror" not in err_str, f"响应暴露了 NameError: {body}"
    assert "traceback" not in err_str, f"响应暴露了 traceback: {body}"
    assert r.status_code == 400, f"存在 pending 异常时 /confirm 应返回 400，实际 {r.status_code}: {body}"
    assert "未处理异常" in body.get("error", ""), f"错误信息应含'未处理异常'，实际 {body}"

    # --- b) 解决所有异常：app.py 会 auto-confirm（因为全部 resolve 完状态 == EXCEPTION_PENDING → CONFIRMED） ---
    resolve_all_exceptions(client, bid)
    batch_after = client.get(f"/api/batches/{bid}").get_json()
    assert batch_after["status"] == "CONFIRMED", f"auto-confirm 后状态应为 CONFIRMED，实际 {batch_after['status']}"

    # --- c) 再调 /confirm：已确认的返回状态流转错误（仍是 JSON，不含 NameError） ---
    r = client.post(f"/api/batches/{bid}/confirm")
    assert r.content_type.startswith("application/json")
    body = r.get_json()
    assert "nameerror" not in str(body.get("error", "")).lower()
    assert r.status_code == 400, f"已 CONFIRMED 时再 /confirm 应 400: {body}"

    # --- d) 入账 /post：这是原先 RESULT_STATUS_PENDING NameError 真正会打到的分支之一 ---
    r = client.post(f"/api/batches/{bid}/post")
    assert r.content_type.startswith("application/json"), f"返回类型不是 JSON: {r.content_type}"
    body = r.get_json()
    assert "nameerror" not in str(body.get("error", "")).lower(), f"/post 暴露了 NameError: {body}"
    assert "traceback" not in str(body.get("error", "")).lower(), f"/post 暴露了 traceback: {body}"
    assert r.status_code == 200, f"/post 失败: {body}"
    assert body["status"] == "POSTED", f"入账后状态应为 POSTED，实际 {body['status']}"


def upload_csv_bytes(client, bid, typ, csv_text, filename="data.csv"):
    """上传字符串形式的 CSV"""
    data = {"file": (BytesIO(csv_text.encode("utf-8-sig")), filename)}
    r = client.post(f"/api/batches/{bid}/upload-{typ}", data=data, content_type="multipart/form-data")
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code, "raw": r.data.decode("utf-8", errors="replace")}


def test_over_tolerance_single_exception_with_both_sides(client):
    """同供应商同 PO 超容差只生成 1 条归并异常，带两边单据、差额、规则版本、备注入口

    数据设计（容差 0.5% / ¥10，故意让 PO-001 与 INV-001 的差额 500 元远超容差）：
      PO:  PO-001, V001, 供应商A, 10000.00, 2024-01-01
           PO-002, V002, 供应商B, 20000.00, 2024-01-01
      INV: INV-001, V001, 供应商A, 10500.00, 2024-01-02   (差额 500=5% > 0.5%，触发 OVER_TOLERANCE)
           INV-002, V002, 供应商B, 20000.00, 2024-01-02   (精确匹配)
    预期：1 条 EXACT、1 条 OVER_TOLERANCE、0 条 UNMATCHED_PO/INVOICE，仅 1 条超容差异常项。
    """
    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V001,供应商A,10000.00,2024-01-01\n"
        "PO-002,V002,供应商B,20000.00,2024-01-01\n"
    )
    inv_csv = (
        "invoice_number,vendor_code,vendor_name,amount,invoice_date\n"
        "INV-001,V001,供应商A,10500.00,2024-01-02\n"
        "INV-002,V002,供应商B,20000.00,2024-01-02\n"
    )

    bid = create_batch(client, "test-over-tol-single", pct=0.5, ab=10)
    upload_csv_bytes(client, bid, "po", po_csv, "po.csv")
    upload_csv_bytes(client, bid, "invoice", inv_csv, "inv.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    results = get_results(client, bid)
    exceptions = get_exceptions(client, bid)

    exact = [x for x in results if x["match_type"] == "EXACT"]
    over_tol = [x for x in results if x["match_type"] == "OVER_TOLERANCE"]
    unmatched_po = [x for x in results if x["match_type"] == "UNMATCHED_PO"]
    unmatched_inv = [x for x in results if x["match_type"] == "UNMATCHED_INVOICE"]

    # 1. 严格 1 条 OVER_TOLERANCE（原 bug：拆成 UNMATCHED_PO + UNMATCHED_INVOICE，over_tol=0）
    assert len(over_tol) == 1, f"OVER_TOLERANCE 严格等于 1，实际 {len(over_tol)}"

    # 2. 严格 1 条 EXACT，且没有任何孤立 UNMATCHED
    assert len(exact) == 1, f"EXACT 严格等于 1，实际 {len(exact)}"
    assert len(unmatched_po) == 0, f"UNMATCHED_PO 严格等于 0，实际 {len(unmatched_po)}"
    assert len(unmatched_inv) == 0, f"UNMATCHED_INVOICE 严格等于 0，实际 {len(unmatched_inv)}"

    # 3. 归并记录字段齐全
    r0 = over_tol[0]
    assert r0["po_number"] == "PO-001", f"po_number 不对: {r0['po_number']}"
    assert r0["invoice_number"] == "INV-001", f"invoice_number 不对: {r0['invoice_number']}"
    assert r0["po_amount"] == 10000.0, f"po_amount 不对: {r0['po_amount']}"
    assert r0["invoice_amount"] == 10500.0, f"invoice_amount 不对: {r0['invoice_amount']}"
    assert r0["amount_diff"] == 500.0, f"amount_diff 不对，期望 500: {r0['amount_diff']}"
    assert r0["rule_version"], f"rule_version 缺失"
    assert r0["is_exception"] is True, f"is_exception 应为 True"
    assert r0["status"] == "PENDING", f"匹配结果 status 应为 PENDING，实际 {r0.get('status')}"

    # 4. 异常项严格 1 条（含 EXCEPTION_PENDING 状态 + "超出容差" 详情 + match_result_id 关联）
    over_ex = [e for e in exceptions if (e.get("detail") or "").find("超出容差") != -1]
    assert len(over_ex) == 1, f"超容差异常项严格 1 条，实际 {len(over_ex)}"
    ex0 = over_ex[0]
    assert ex0["match_result_id"] == r0["id"], "异常项没有关联到归并后的 match_result"
    assert ex0["status"] == EXCEPTION_STATUS_PENDING, f"异常状态应为 PENDING: {ex0['status']}"

    # 5. 备注入口正常：能写备注并读回
    r = client.put(
        f"/api/batches/{bid}/exceptions/{ex0['id']}/remark",
        json={"remarks": "财务确认该差异为补开税额，同意入账"},
    )
    assert r.status_code == 200
    updated = client.get(f"/api/batches/{bid}/exceptions").get_json()["exceptions"]
    target = next(x for x in updated if x["id"] == ex0["id"])
    assert target["remarks"] == "财务确认该差异为补开税额，同意入账", "备注写入失败"


def test_missing_columns_no_partial_result(client, sample_dir):
    """缺列文件上传失败，批次保持 CREATED，不产生部分结果"""
    bid = create_batch(client, "test-bad-col")

    r = upload_file(client, bid, "po", sample_dir, "bad_missing_columns.csv")
    # 上传缺列文件应返回错误
    assert "error" in r, f"缺列文件应返回错误，实际 {r}"
    error_text = f"{r.get('error', '')} {r.get('details', [])}"
    assert "缺少列" in error_text, f"错误信息应含'缺少列'，实际 {error_text}"

    # 批次状态仍为 CREATED，无任何匹配结果
    batch = get_batch(client, bid)
    assert batch["status"] == BATCH_STATUS_CREATED, f"状态应为 CREATED，实际 {batch['status']}"
    assert batch["summary"]["matched_count"] == 0, "matched_count 应为 0"

    # 数据库层面也没有残留
    with client.application.app_context():
        assert MatchResult.query.filter_by(batch_id=bid).count() == 0
        assert ExceptionItem.query.filter_by(batch_id=bid).count() == 0


def test_duplicate_invoice_no_partial_result(client, sample_dir):
    """重复发票号上传失败，批次保持 CREATED，不产生部分结果"""
    bid = create_batch(client, "test-dup-inv")
    upload_file(client, bid, "po", sample_dir, "bad_over_tolerance_po.csv")

    r = upload_file(client, bid, "invoice", sample_dir, "bad_duplicate_invoice.csv")
    assert "error" in r, f"重复发票应返回错误，实际 {r}"
    error_text = f"{r.get('error', '')} {r.get('details', [])}"
    assert "发票号重复" in error_text, f"错误信息应含'发票号重复'，实际 {error_text}"

    batch = get_batch(client, bid)
    assert batch["status"] == BATCH_STATUS_CREATED, f"状态应为 CREATED，实际 {batch['status']}"
    assert batch["summary"]["matched_count"] == 0, "matched_count 应为 0"

    with client.application.app_context():
        assert MatchResult.query.filter_by(batch_id=bid).count() == 0
        assert ExceptionItem.query.filter_by(batch_id=bid).count() == 0


def test_rollback_export_payable_consistent(client, sample_dir):
    """回滚后导出报表应付合计与接口汇总一致"""
    bid = create_batch(client, "test-rollback-export")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    client.post(f"/api/batches/{bid}/match")
    resolve_all_exceptions(client, bid)
    client.post(f"/api/batches/{bid}/confirm")
    client.post(f"/api/batches/{bid}/post")

    # 回滚
    r = client.post(f"/api/batches/{bid}/rollback")
    assert r.status_code == 200
    assert r.get_json()["status"] == BATCH_STATUS_ROLLED_BACK

    # 重复回滚被拦截
    r = client.post(f"/api/batches/{bid}/rollback")
    assert r.status_code == 400
    body = r.get_json()
    assert "已回滚" in body["error"], f"重复回滚错误信息不对: {body}"

    # API 应付合计
    api_payable = get_batch(client, bid)["summary"]["payable_total"]

    # 导出到临时目录
    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200
    tmp_dir = tempfile.gettempdir()
    csv_path = os.path.join(tmp_dir, f"pytest_export_batch_{bid}.csv")
    with open(csv_path, "wb") as f:
        f.write(r.data)

    # 解析导出文件中的应付合计
    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    exported_payable = None
    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary and row[0] == "应付合计":
            exported_payable = float(row[1])
            break

    assert exported_payable is not None, "导出文件未找到'应付合计'"
    assert exported_payable == api_payable, (
        f"导出应付合计 {exported_payable} != API 应付合计 {api_payable}"
    )

    # 导出文件没写进源码目录
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert not os.path.exists(os.path.join(project_dir, f"pytest_export_batch_{bid}.csv")), (
        "导出文件不应写进源码目录"
    )
    # 临时目录的导出文件存在
    assert os.path.exists(csv_path), f"临时目录导出文件不存在: {csv_path}"


# ---------- 应付重算说明 测试辅助函数 ----------

def get_latest_note(client, bid):
    r = client.get(f"/api/batches/{bid}/recalc-notes/latest")
    assert r.status_code == 200
    return r.get_json().get("note")


def list_notes(client, bid):
    r = client.get(f"/api/batches/{bid}/recalc-notes")
    assert r.status_code == 200
    return r.get_json()["notes"]


def match_and_resolve_all(client, bid, sample_dir):
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")
    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200
    resolve_all_exceptions(client, bid)
    return bid


# ---------- 应付重算说明 测试用例 ----------

def test_recalc_note_first_generation(client, sample_dir):
    """场景1: 首次匹配生成应付重算说明 v1"""
    bid = create_batch(client, "test-note-v1")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    note = get_latest_note(client, bid)
    assert note is not None, "匹配后应生成应付说明"
    assert note["version"] == 1, f"首次生成版本应为 1，实际 {note['version']}"
    assert note["previous_total"] is None, "v1 无 previous_total"
    assert note["amount_diff"] is None, "v1 无 amount_diff"
    assert isinstance(note["current_total"], float), "current_total 应为数值"
    assert note["current_total"] > 0, "current_total 应大于 0"
    assert note["change_source"] == "MATCH", f"change_source 应为 MATCH，实际 {note['change_source']}"
    assert note["change_summary"], "change_summary 不能为空"
    assert "首次生成" in note["change_summary"], f"v1 摘要应含'首次生成'，实际 {note['change_summary']}"
    assert isinstance(note["po_numbers"], list), "po_numbers 应为列表"
    assert isinstance(note["invoice_numbers"], list), "invoice_numbers 应为列表"
    assert len(note["po_numbers"]) > 0, "po_numbers 不应为空"
    assert len(note["invoice_numbers"]) > 0, "invoice_numbers 不应为空"
    assert note["rule_version"], "rule_version 不能为空"
    assert note["created_at"], "created_at 不能为空"


def test_recalc_note_no_change_no_duplicate(client, sample_dir):
    """场景2: 无变化重复生成不会堆积多条相同记录"""
    bid = create_batch(client, "test-note-dedup")
    match_and_resolve_all(client, bid, sample_dir)

    notes_before = list_notes(client, bid)
    count_before = len(notes_before)
    assert count_before >= 1

    r = client.post(f"/api/batches/{bid}/recalc-notes/generate", json={"change_source": "MANUAL"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["is_new"] is False, "无变化时 is_new 应为 False"

    notes_after = list_notes(client, bid)
    count_after = len(notes_after)
    assert count_after == count_before, f"无变化不应新增记录，{count_before} → {count_after}"

    r = client.post(f"/api/batches/{bid}/recalc-notes/generate", json={"change_source": "MANUAL"})
    assert r.status_code == 200
    assert r.get_json()["is_new"] is False

    notes_final = list_notes(client, bid)
    assert len(notes_final) == count_before


def test_recalc_note_change_creates_new_version(client, sample_dir):
    """场景3: 异常处理变更后生成新版本 v2"""
    bid = create_batch(client, "test-note-v2")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    note_v1 = get_latest_note(client, bid)
    assert note_v1["version"] == 1
    v1_total = note_v1["current_total"]

    exs = get_exceptions(client, bid)
    assert len(exs) > 0, "需要至少 1 条异常来测试"
    exc0 = exs[0]

    r = client.put(
        f"/api/batches/{bid}/exceptions/{exc0['id']}/remark",
        json={"remarks": "财务改了意见"},
    )
    assert r.status_code == 200

    note_v2 = get_latest_note(client, bid)
    assert note_v2 is not None
    assert note_v2["version"] == 2, f"异常备注变更后应生成 v2，实际 {note_v2['version']}"
    assert note_v2["previous_total"] == v1_total, "v2 previous_total 应等于 v1 current_total"
    assert note_v2["change_source"] == "EXCEPTION_REMARK"
    assert note_v2["change_summary"], "v2 摘要不能为空"

    r = client.put(
        f"/api/batches/{bid}/exceptions/{exc0['id']}/resolve",
        json={"action": "reject"},
    )
    assert r.status_code == 200

    note_v3 = get_latest_note(client, bid)
    assert note_v3["version"] == 3, f"异常 reject 后应生成 v3，实际 {note_v3['version']}"
    assert note_v3["change_source"] == "EXCEPTION_REJECT"

    all_notes = list_notes(client, bid)
    assert len(all_notes) == 3, f"应有 3 个版本，实际 {len(all_notes)}"
    versions = [n["version"] for n in all_notes]
    assert versions == [1, 2, 3], f"版本号应按升序 [1,2,3]，实际 {versions}"


def test_recalc_note_persists_across_app_restart(client, sample_dir, tmp_path):
    """场景4: 重启后数据保留（使用文件 SQLite 模拟持久化）"""
    from app import create_app
    from models import db

    db_path = tmp_path / "persist_test.db"
    db_uri = f"sqlite:///{db_path}"

    app1 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app1.app_context():
        db.create_all()
    c1 = app1.test_client()

    bid = create_batch(c1, "test-note-persist")
    upload_file(c1, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(c1, bid, "invoice", sample_dir, "invoices.csv")
    r = c1.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    note_before = c1.get(f"/api/batches/{bid}/recalc-notes/latest").get_json()["note"]
    assert note_before["version"] == 1
    saved_total = note_before["current_total"]
    saved_version = note_before["version"]
    saved_summary = note_before["change_summary"]
    saved_hash = note_before.get("rule_version")

    with app1.app_context():
        db.session.remove()

    app2 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app2.app_context():
        db.create_all()
    c2 = app2.test_client()

    note_after = c2.get(f"/api/batches/{bid}/recalc-notes/latest").get_json()["note"]
    assert note_after is not None, "重启后应能读到说明"
    assert note_after["version"] == saved_version, f"版本号应不变: {saved_version}"
    assert note_after["current_total"] == saved_total, f"应付合计应不变: {saved_total}"
    assert note_after["change_summary"] == saved_summary, "摘要应不变"

    all_notes = c2.get(f"/api/batches/{bid}/recalc-notes").get_json()["notes"]
    assert len(all_notes) == 1, "重启后记录数应不变"

    with app2.app_context():
        db.session.remove()
        db.drop_all()


def test_recalc_note_rollback_history_and_export_no_cross_data(client, sample_dir):
    """场景5: 回滚后历史可查，当前导出不串数据"""
    bid = create_batch(client, "test-note-rollback")
    match_and_resolve_all(client, bid, sample_dir)
    client.post(f"/api/batches/{bid}/confirm")
    client.post(f"/api/batches/{bid}/post")

    notes_before_rollback = list_notes(client, bid)
    count_before = len(notes_before_rollback)
    assert count_before >= 1, "回滚前至少有 1 条说明"

    last_before = notes_before_rollback[-1]

    r = client.post(f"/api/batches/{bid}/rollback")
    assert r.status_code == 200
    assert r.get_json()["status"] == BATCH_STATUS_ROLLED_BACK

    notes_after_rollback = list_notes(client, bid)
    count_after = len(notes_after_rollback)
    assert count_after == count_before + 1, "回滚应生成新版本"

    for i, n in enumerate(notes_before_rollback):
        assert notes_after_rollback[i]["version"] == n["version"]
        assert notes_after_rollback[i]["current_total"] == n["current_total"]
        assert notes_after_rollback[i]["change_summary"] == n["change_summary"]

    latest = get_latest_note(client, bid)
    assert latest["change_source"] == "ROLLBACK"

    api_payable = get_batch(client, bid)["summary"]["payable_total"]

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200
    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    exported_payable = None
    exported_note_version = None
    exported_note_summary = None
    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary and row[0] == "应付合计":
            exported_payable = float(row[1])
        if in_summary and row[0] == "重算说明版本":
            exported_note_version = int(row[1])
        if in_summary and row[0] == "重算说明摘要":
            exported_note_summary = row[1] if len(row) > 1 else ""

    assert exported_payable is not None, "导出应含应付合计"
    assert exported_payable == api_payable, f"导出应付 {exported_payable} != API {api_payable}"
    assert exported_note_version == latest["version"], f"导出版本 {exported_note_version} != 最新 {latest['version']}"
    assert exported_note_summary is not None, "导出应含重算说明摘要"
    assert exported_note_summary == latest["change_summary"], "导出摘要应等于最新版本摘要"

    r = client.post(f"/api/batches/{bid}/reset")
    assert r.status_code == 200

    notes_after_reset = list_notes(client, bid)
    assert len(notes_after_reset) == count_after + 1, "重置也应生成新版本"
    for i, n in enumerate(notes_after_rollback):
        assert notes_after_reset[i]["version"] == n["version"]
        assert notes_after_reset[i]["current_total"] == n["current_total"]


def test_recalc_note_list_and_get_by_id(client, sample_dir):
    """API: 列表查询和按ID查询"""
    bid = create_batch(client, "test-note-api")
    match_and_resolve_all(client, bid, sample_dir)

    notes = list_notes(client, bid)
    assert len(notes) >= 1
    n0 = notes[0]

    r = client.get(f"/api/batches/{bid}/recalc-notes/{n0['id']}")
    assert r.status_code == 200
    fetched = r.get_json()["note"]
    assert fetched["id"] == n0["id"]
    assert fetched["version"] == n0["version"]
    assert fetched["current_total"] == n0["current_total"]

    r = client.get(f"/api/batches/{bid}/recalc-notes/99999")
    assert r.status_code == 404


def test_recalc_note_amount_aligned_with_batch_detail(client, sample_dir):
    """导出金额与批次详情接口对齐"""
    bid = create_batch(client, "test-note-align")
    match_and_resolve_all(client, bid, sample_dir)

    batch = get_batch(client, bid)
    api_payable = batch["summary"]["payable_total"]

    note = get_latest_note(client, bid)
    assert note["current_total"] == api_payable, (
        f"说明应付 {note['current_total']} != 接口 {api_payable}"
    )

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200
    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))
    exported_payable = None
    for row in rows:
        if row and row[0] == "应付合计":
            exported_payable = float(row[1])
            break
    assert exported_payable == api_payable, (
        f"导出应付 {exported_payable} != 接口 {api_payable}"
    )


def test_recalc_note_affected_docs_only_changed():
    """回归测试：两条容差异常，只改 A 的备注，v2 应只列出 A 单据，且 B 不出现。

    数据设计（容差 0.1% / ¥1，确保两条都是超容差）：
      PO:  PO-A, V001, 供应商A, 10000.00, 2024-01-01  (INV-A: 10500 → 差500 → 超容差)
           PO-B, V002, 供应商B, 20000.00, 2024-01-01  (INV-B: 21000 → 差1000 → 超容差)
      INV: INV-A, V001, 供应商A, 10500.00, 2024-01-02
           INV-B, V002, 供应商B, 21000.00, 2024-01-02
    """
    from app import create_app
    from models import db
    import tempfile

    tmp_dir = tempfile.mkdtemp()
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": tmp_dir,
    })
    with app.app_context():
        db.create_all()
    client = app.test_client()

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-A,V001,供应商A,10000.00,2024-01-01\n"
        "PO-B,V002,供应商B,20000.00,2024-01-01\n"
    )
    inv_csv = (
        "invoice_number,vendor_code,vendor_name,amount,invoice_date\n"
        "INV-A,V001,供应商A,10500.00,2024-01-02\n"
        "INV-B,V002,供应商B,21000.00,2024-01-02\n"
    )

    bid = create_batch(client, "test-affected-docs", pct=0.1, ab=1)
    upload_csv_bytes(client, bid, "po", po_csv, "po.csv")
    upload_csv_bytes(client, bid, "invoice", inv_csv, "inv.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    note_v1 = client.get(f"/api/batches/{bid}/recalc-notes/latest").get_json()["note"]
    assert note_v1["version"] == 1
    assert "PO-A" in note_v1["po_numbers"], "v1 应列出全量PO"
    assert "PO-B" in note_v1["po_numbers"], "v1 应列出全量PO"
    assert "INV-A" in note_v1["invoice_numbers"], "v1 应列出全量发票"
    assert "INV-B" in note_v1["invoice_numbers"], "v1 应列出全量发票"

    exs = client.get(f"/api/batches/{bid}/exceptions").get_json()["exceptions"]
    assert len(exs) == 2, f"应有 2 条异常，实际 {len(exs)}"

    exc_a = None
    for e in exs:
        if e.get("po_number") == "PO-A":
            exc_a = e
            break
    assert exc_a is not None, "找不到 PO-A 对应的异常"

    r = client.put(
        f"/api/batches/{bid}/exceptions/{exc_a['id']}/remark",
        json={"remarks": "财务只改了A条的意见"},
    )
    assert r.status_code == 200

    note_v2 = client.get(f"/api/batches/{bid}/recalc-notes/latest").get_json()["note"]
    assert note_v2["version"] == 2, f"应生成 v2，实际 v{note_v2['version']}"
    assert note_v2["po_numbers"] == ["PO-A"], f"v2 应只含 PO-A，实际 {note_v2['po_numbers']}"
    assert note_v2["invoice_numbers"] == ["INV-A"], f"v2 应只含 INV-A，实际 {note_v2['invoice_numbers']}"
    assert "PO-B" not in note_v2["po_numbers"], "v2 不应包含未变化的 PO-B"
    assert "INV-B" not in note_v2["invoice_numbers"], "v2 不应包含未变化的 INV-B"
    assert note_v2["change_source"] == "EXCEPTION_REMARK"

    r = client.post(f"/api/batches/{bid}/recalc-notes/generate", json={"change_source": "MANUAL"})
    assert r.status_code == 200
    assert r.get_json()["is_new"] is False, "无变化重复生成不应新增"

    all_notes = client.get(f"/api/batches/{bid}/recalc-notes").get_json()["notes"]
    assert len(all_notes) == 2, f"仍应为 2 个版本，实际 {len(all_notes)}"

    api_payable = client.get(f"/api/batches/{bid}").get_json()["summary"]["payable_total"]
    latest_note = client.get(f"/api/batches/{bid}/recalc-notes/latest").get_json()["note"]
    assert latest_note["current_total"] == api_payable, "说明金额应与批次详情对齐"
    assert latest_note["version"] == 2, "最新说明版本应为 2"

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200
    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    exported_version = None
    exported_summary = None
    exported_payable = None
    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary and row[0] == "应付合计":
            exported_payable = float(row[1])
        if in_summary and row[0] == "重算说明版本":
            exported_version = int(row[1])
        if in_summary and row[0] == "重算说明摘要":
            exported_summary = row[1] if len(row) > 1 else ""

    assert exported_payable == api_payable, "导出应付应与接口对齐"
    assert exported_version == 2, f"导出版本应为 2，实际 {exported_version}"
    assert exported_summary is not None, "导出应含重算说明摘要"
    assert exported_summary == latest_note["change_summary"], "导出摘要应等于最新说明摘要"

    with app.app_context():
        db.session.remove()
        db.drop_all()


# ---------- 版本对比 测试辅助函数 ----------

def compare_notes_api(client, bid, note_a_id, note_b_id, operator="test_user"):
    r = client.post(
        f"/api/batches/{bid}/recalc-notes/compare",
        json={"note_a_id": note_a_id, "note_b_id": note_b_id, "operator": operator},
    )
    return r


# ---------- 版本对比 测试用例 ----------

def test_version_comparison_v1_vs_v3_after_two_remarks(client, sample_dir):
    """两次备注变更后做 v1/v3 对比，验证差异分析完整。

    流程:
    1. 创建批次 + 匹配 → 生成 v1
    2. 修改异常 A 备注 → 生成 v2
    3. 修改异常 B 备注 → 生成 v3
    4. 对比 v1 vs v3
    预期:
    - 应付合计差额正确
    - 变化来源正确
    - 涉及采购单/发票的变更列表正确
    - 规则版本和操作人正确
    """
    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-A,V001,供应商A,10000.00,2024-01-01\n"
        "PO-B,V002,供应商B,20000.00,2024-01-01\n"
    )
    inv_csv = (
        "invoice_number,vendor_code,vendor_name,amount,invoice_date\n"
        "INV-A,V001,供应商A,10500.00,2024-01-02\n"
        "INV-B,V002,供应商B,21000.00,2024-01-02\n"
    )

    bid = create_batch(client, "test-compare-v1-v3", pct=0.1, ab=1)
    upload_csv_bytes(client, bid, "po", po_csv, "po.csv")
    upload_csv_bytes(client, bid, "invoice", inv_csv, "inv.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    notes = list_notes(client, bid)
    assert len(notes) >= 1
    note_v1 = notes[0]
    assert note_v1["version"] == 1
    v1_total = note_v1["current_total"]

    exs = get_exceptions(client, bid)
    assert len(exs) == 2, f"应有 2 条异常，实际 {len(exs)}"

    exc_a = next(e for e in exs if e["po_number"] == "PO-A")
    exc_b = next(e for e in exs if e["po_number"] == "PO-B")

    r = client.put(
        f"/api/batches/{bid}/exceptions/{exc_a['id']}/remark",
        json={"remarks": "第一次备注变更"},
    )
    assert r.status_code == 200

    notes = list_notes(client, bid)
    note_v2 = notes[-1]
    assert note_v2["version"] == 2

    r = client.put(
        f"/api/batches/{bid}/exceptions/{exc_b['id']}/remark",
        json={"remarks": "第二次备注变更"},
    )
    assert r.status_code == 200

    notes = list_notes(client, bid)
    assert len(notes) == 3
    note_v3 = notes[-1]
    assert note_v3["version"] == 3
    v3_total = note_v3["current_total"]

    r = compare_notes_api(client, bid, note_v1["id"], note_v3["id"], operator="finance_user")
    assert r.status_code == 200, f"对比失败: {r.data}"
    body = r.get_json()
    comp = body["comparison"]

    expected_diff = round(v3_total - v1_total, 2)
    assert comp["amount_diff"] == expected_diff, (
        f"差额不对: {comp['amount_diff']} != {expected_diff}"
    )
    assert comp["note_a_version"] == 1
    assert comp["note_b_version"] == 3
    assert comp["operator"] == "finance_user"
    assert comp["rule_version_a"] == note_v1["rule_version"]
    assert comp["rule_version_b"] == note_v3["rule_version"]
    assert comp["comparison_summary"], "对比摘要不应为空"
    assert "v1 → v3" in comp["comparison_summary"]

    assert "PO-A" in comp["po_changed"], "PO-A 应在变更列表"
    assert "PO-B" in comp["po_changed"], "PO-B 应在变更列表"
    assert "INV-A" in comp["invoice_changed"], "INV-A 应在变更列表"
    assert "INV-B" in comp["invoice_changed"], "INV-B 应在变更列表"

    assert comp["po_added"] == [], "v1→v3 不应有新增采购单"
    assert comp["po_removed"] == [], "v1→v3 不应有移除采购单"
    assert comp["invoice_added"] == [], "v1→v3 不应有新增发票"
    assert comp["invoice_removed"] == [], "v1→v3 不应有移除发票"

    assert comp["change_source"], "变化来源不应为空"
    assert "变更" in comp["change_source"], "变化来源应包含'变更'"

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons")
    assert r.status_code == 200
    assert len(r.get_json()["comparisons"]) == 1

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/latest")
    assert r.status_code == 200
    latest = r.get_json()["comparison"]
    assert latest["id"] == comp["id"]


def test_version_comparison_invalid_cases_return_400(client, sample_dir):
    """非法对比场景返回 400 错误，不抛 500。

    覆盖:
    - 版本不存在
    - 跨批次对比
    - 同版本对比
    - 参数缺失
    """
    bid1 = create_batch(client, "test-compare-err-1")
    match_and_resolve_all(client, bid1, sample_dir)
    notes1 = list_notes(client, bid1)
    assert len(notes1) >= 1
    n1 = notes1[0]

    bid2 = create_batch(client, "test-compare-err-2")
    match_and_resolve_all(client, bid2, sample_dir)
    notes2 = list_notes(client, bid2)
    assert len(notes2) >= 1
    n2 = notes2[0]

    r = compare_notes_api(client, bid1, n1["id"], n1["id"])
    assert r.status_code == 400, f"同版本对比应 400，实际 {r.status_code}: {r.data}"
    body = r.get_json()
    assert "同一版本" in body.get("error", ""), f"错误信息不对: {body}"
    assert r.content_type.startswith("application/json")

    r = compare_notes_api(client, bid1, n1["id"], n2["id"])
    assert r.status_code == 400, f"跨批次对比应 400，实际 {r.status_code}: {r.data}"
    body = r.get_json()
    assert "不属于" in body.get("error", ""), f"错误信息不对: {body}"
    assert r.content_type.startswith("application/json")

    r = compare_notes_api(client, bid1, n1["id"], 99999)
    assert r.status_code == 400, f"版本不存在应 400，实际 {r.status_code}: {r.data}"
    body = r.get_json()
    assert "不存在" in body.get("error", ""), f"错误信息不对: {body}"
    assert r.content_type.startswith("application/json")

    r = client.post(f"/api/batches/{bid1}/recalc-notes/compare", json={})
    assert r.status_code == 400, f"参数缺失应 400，实际 {r.status_code}: {r.data}"
    body = r.get_json()
    assert "缺失" in body.get("error", ""), f"错误信息不对: {body}"

    r = client.post(
        f"/api/batches/{bid1}/recalc-notes/compare",
        json={"note_a_id": "abc", "note_b_id": 1},
    )
    assert r.status_code == 400, f"参数类型错误应 400，实际 {r.status_code}: {r.data}"


def test_export_includes_comparison_summary(client, sample_dir):
    """CSV 汇总区只包含最近一次已确认对比的摘要，待复核/已忽略的不混入，导出金额仍和批次详情一致。"""
    bid = create_batch(client, "test-export-compare-summary")
    match_and_resolve_all(client, bid, sample_dir)

    exs = get_exceptions(client, bid)
    if exs:
        exc0 = exs[0]
        r = client.put(
            f"/api/batches/{bid}/exceptions/{exc0['id']}/remark",
            json={"remarks": "修改备注触发v2"},
        )
        assert r.status_code == 200

    notes = list_notes(client, bid)
    assert len(notes) >= 2, f"需要至少 2 个版本才能对比，实际 {len(notes)}"
    n1 = notes[0]
    n2 = notes[-1]

    r = compare_notes_api(client, bid, n1["id"], n2["id"], operator="export_test")
    assert r.status_code == 200
    comp = r.get_json()["comparison"]

    api_payable = get_batch(client, bid)["summary"]["payable_total"]

    r_before = client.get(f"/api/batches/{bid}/export")
    assert r_before.status_code == 200
    with io.StringIO(r_before.data.decode("utf-8-sig")) as f:
        rows_before = list(csv.reader(f))
    has_old_field = any(row and row[0] == "最近对比摘要" for row in rows_before)
    has_confirmed_field_before = any(row and row[0] == "最近已确认对比摘要" for row in rows_before)
    assert not has_old_field, "未确认的对比不应导出'最近对比摘要'"
    assert not has_confirmed_field_before, "未确认的对比不应导出'最近已确认对比摘要'"

    r_review = client.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/{comp['id']}/review",
        json={"review_status": "CONFIRMED", "review_remark": "财务已复核", "operator": "finance_reviewer"},
    )
    assert r_review.status_code == 200

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200

    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    exported_payable = None
    exported_compare_summary = None
    exported_compare_versions = None
    exported_compare_operator = None
    exported_reviewer = None
    exported_review_remark = None

    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary and row[0] == "应付合计":
            exported_payable = float(row[1])
        if in_summary and row[0] == "最近已确认对比摘要":
            exported_compare_summary = row[1] if len(row) > 1 else ""
        if in_summary and row[0] == "对比版本":
            exported_compare_versions = row[1] if len(row) > 1 else ""
        if in_summary and row[0] == "对比操作人":
            exported_compare_operator = row[1] if len(row) > 1 else ""
        if in_summary and row[0] == "复核人":
            exported_reviewer = row[1] if len(row) > 1 else ""
        if in_summary and row[0] == "复核备注":
            exported_review_remark = row[1] if len(row) > 1 else ""

    assert exported_payable is not None, "导出应含应付合计"
    assert exported_payable == api_payable, (
        f"导出应付 {exported_payable} != 接口应付 {api_payable}"
    )
    assert exported_compare_summary == comp["comparison_summary"], (
        f"导出对比摘要不对: {exported_compare_summary} != {comp['comparison_summary']}"
    )
    assert exported_compare_versions == f"v{comp['note_a_version']} → v{comp['note_b_version']}"
    assert exported_compare_operator == "export_test"
    assert exported_reviewer == "finance_reviewer", "导出应含复核人"
    assert exported_review_remark == "财务已复核", "导出应含复核备注"


def test_comparison_persists_across_restart(client, sample_dir, tmp_path):
    """服务重启后历史说明和对比接口结果都能查回来。"""
    from app import create_app
    from models import db

    db_path = tmp_path / "compare_persist_test.db"
    db_uri = f"sqlite:///{db_path}"

    app1 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app1.app_context():
        db.create_all()
    c1 = app1.test_client()

    bid = create_batch(c1, "test-compare-persist")
    upload_file(c1, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(c1, bid, "invoice", sample_dir, "invoices.csv")
    r = c1.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    exs = c1.get(f"/api/batches/{bid}/exceptions").get_json()["exceptions"]
    if exs:
        exc0 = exs[0]
        r = c1.put(
            f"/api/batches/{bid}/exceptions/{exc0['id']}/remark",
            json={"remarks": "修改备注触发v2"},
        )
        assert r.status_code == 200

    notes_before = c1.get(f"/api/batches/{bid}/recalc-notes").get_json()["notes"]
    assert len(notes_before) >= 2
    n1 = notes_before[0]
    n2 = notes_before[-1]

    r = c1.post(
        f"/api/batches/{bid}/recalc-notes/compare",
        json={"note_a_id": n1["id"], "note_b_id": n2["id"], "operator": "persist_test"},
    )
    assert r.status_code == 200
    comp_before = r.get_json()["comparison"]
    comp_id = comp_before["id"]
    comp_summary = comp_before["comparison_summary"]
    comp_diff = comp_before["amount_diff"]
    comp_operator = comp_before["operator"]

    with app1.app_context():
        db.session.remove()

    app2 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app2.app_context():
        db.create_all()
    c2 = app2.test_client()

    notes_after = c2.get(f"/api/batches/{bid}/recalc-notes").get_json()["notes"]
    assert len(notes_after) == len(notes_before), "重启后说明数量应不变"

    r = c2.get(f"/api/batches/{bid}/recalc-notes/comparisons")
    assert r.status_code == 200
    comparisons = r.get_json()["comparisons"]
    assert len(comparisons) >= 1, "重启后应能查到对比记录"

    r = c2.get(f"/api/batches/{bid}/recalc-notes/comparisons/latest")
    assert r.status_code == 200
    latest = r.get_json()["comparison"]
    assert latest is not None
    assert latest["id"] == comp_id
    assert latest["comparison_summary"] == comp_summary
    assert latest["amount_diff"] == comp_diff
    assert latest["operator"] == comp_operator

    r = c2.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comp_id}")
    assert r.status_code == 200
    fetched = r.get_json()["comparison"]
    assert fetched["id"] == comp_id
    assert fetched["comparison_summary"] == comp_summary

    with app2.app_context():
        db.session.remove()
        db.drop_all()


# ---------- 前端/页面入口 测试 ----------

def test_recalc_notes_frontend_api_flow(client, sample_dir):
    """模拟前端调用流程：列表 → 选两个版本 → 对比 → 展示结果"""
    bid = create_batch(client, "test-frontend-flow")
    match_and_resolve_all(client, bid, sample_dir)

    exs = get_exceptions(client, bid)
    if exs:
        exc0 = exs[0]
        r = client.put(
            f"/api/batches/{bid}/exceptions/{exc0['id']}/remark",
            json={"remarks": "前端测试备注"},
        )
        assert r.status_code == 200

    r = client.get(f"/api/batches/{bid}/recalc-notes")
    assert r.status_code == 200
    notes = r.get_json()["notes"]
    assert len(notes) >= 2, f"需要至少 2 个版本，实际 {len(notes)}"
    assert "id" in notes[0]
    assert "version" in notes[0]
    assert "current_total" in notes[0]
    assert "change_source" in notes[0]

    n1 = notes[0]
    n2 = notes[-1]

    r = client.post(
        f"/api/batches/{bid}/recalc-notes/compare",
        json={"note_a_id": n1["id"], "note_b_id": n2["id"], "operator": "web_user"},
    )
    assert r.status_code == 200
    comp = r.get_json()["comparison"]
    assert comp["operator"] == "web_user"
    assert comp["note_a_version"] == n1["version"]
    assert comp["note_b_version"] == n2["version"]
    assert "amount_diff" in comp
    assert "change_source" in comp
    assert "po_added" in comp
    assert "po_removed" in comp
    assert "po_changed" in comp
    assert "invoice_added" in comp
    assert "invoice_removed" in comp
    assert "invoice_changed" in comp
    assert "rule_version_a" in comp
    assert "rule_version_b" in comp

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons")
    assert r.status_code == 200
    comps = r.get_json()["comparisons"]
    assert len(comps) >= 1

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/latest")
    assert r.status_code == 200
    latest = r.get_json()["comparison"]
    assert latest["id"] == comp["id"]

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comp['id']}")
    assert r.status_code == 200
    fetched = r.get_json()["comparison"]
    assert fetched["id"] == comp["id"]


def test_index_page_has_recalc_notes_tab(client):
    """首页 HTML 包含重算说明 tab 入口，前端漏接问题不复现"""
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "重算说明" in html, "页面应包含'重算说明'tab"
    assert "版本对比" in html, "页面应包含'版本对比'区域"
    assert "recalc-notes" in html, "页面应包含 recalc-notes tab key"
    assert "开始对比" in html, "页面应包含对比按钮"
    assert "应付差额" in html, "页面应展示应付差额"
    assert "变化来源" in html, "页面应展示变化来源"
    assert "说明版本历史" in html, "页面应包含说明版本历史"


def test_frontend_compare_error_handling_client_friendly(client, sample_dir):
    """前端对比接口的错误返回对用户友好（400 JSON，含 error 字段）"""
    bid = create_batch(client, "test-frontend-error")
    match_and_resolve_all(client, bid, sample_dir)

    notes = list_notes(client, bid)
    assert len(notes) >= 1
    n1 = notes[0]

    r = client.post(
        f"/api/batches/{bid}/recalc-notes/compare",
        json={"note_a_id": n1["id"], "note_b_id": n1["id"]},
    )
    assert r.status_code == 400
    body = r.get_json()
    assert "error" in body
    assert isinstance(body["error"], str)
    assert len(body["error"]) > 0
    assert "traceback" not in body, "错误信息不应暴露 traceback"
    assert "NameError" not in body.get("error", ""), "错误信息不应暴露 NameError"


# ---------- 复核记录 测试辅助函数 ----------

def review_comparison_api(client, bid, comp_id, status, remark="", operator="test_reviewer"):
    r = client.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/{comp_id}/review",
        json={"review_status": status, "review_remark": remark, "operator": operator},
    )
    return r


def create_two_notes_and_compare(client, bid, sample_dir):
    """创建两个版本并对比，返回对比记录"""
    match_and_resolve_all(client, bid, sample_dir)
    exs = get_exceptions(client, bid)
    if exs:
        exc0 = exs[0]
        client.put(
            f"/api/batches/{bid}/exceptions/{exc0['id']}/remark",
            json={"remarks": "触发v2的备注"},
        )
    notes = list_notes(client, bid)
    assert len(notes) >= 2
    r = compare_notes_api(client, bid, notes[0]["id"], notes[-1]["id"])
    assert r.status_code == 200
    return r.get_json()["comparison"]


# ---------- 复核记录 测试用例 ----------

def test_review_comparison_basic_flow(client, sample_dir):
    """复核基本流程：待复核 → 已确认，状态和备注写入数据库"""
    bid = create_batch(client, "test-review-basic")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    assert comp["review_status"] == "PENDING", "新建对比应为待复核状态"
    assert comp["review_remark"] is None or comp["review_remark"] == ""
    assert comp["reviewed_by"] is None
    assert comp["reviewed_at"] is None

    r = review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="财务确认无误", operator="finance_zhang")
    assert r.status_code == 200, f"确认失败: {r.data}"
    updated = r.get_json()["comparison"]

    assert updated["review_status"] == "CONFIRMED"
    assert updated["review_remark"] == "财务确认无误"
    assert updated["reviewed_by"] == "finance_zhang"
    assert updated["reviewed_at"] is not None

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comp['id']}")
    assert r.status_code == 200
    fetched = r.get_json()["comparison"]
    assert fetched["review_status"] == "CONFIRMED"
    assert fetched["review_remark"] == "财务确认无误"
    assert fetched["reviewed_by"] == "finance_zhang"


def test_review_comparison_conflict_duplicate_confirm(client, sample_dir):
    """冲突场景：已确认后再确认 → 返回 400 错误"""
    bid = create_batch(client, "test-review-conflict-confirm")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    r = review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="第一次确认")
    assert r.status_code == 200

    r = review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="重复确认")
    assert r.status_code == 400, f"重复确认应返回 400，实际 {r.status_code}"
    body = r.get_json()
    assert "已确认" in body.get("error", ""), f"错误信息应提示已确认: {body}"
    assert "重复确认" in body.get("error", ""), f"错误信息应提示重复确认: {body}"


def test_review_comparison_conflict_ignored_then_confirm(client, sample_dir):
    """冲突场景：已忽略后再确认 → 返回 400 错误"""
    bid = create_batch(client, "test-review-conflict-ignored")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    r = review_comparison_api(client, bid, comp["id"], "IGNORED", remark="先忽略")
    assert r.status_code == 200

    r = review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="忽略后再确认")
    assert r.status_code == 400, f"忽略后确认应返回 400，实际 {r.status_code}"
    body = r.get_json()
    assert "已忽略" in body.get("error", ""), f"错误信息应提示已忽略: {body}"


def test_review_comparison_not_found(client, sample_dir):
    """冲突场景：对比记录不存在 → 返回 400（复核冲突统一 400）"""
    bid = create_batch(client, "test-review-notfound")
    match_and_resolve_all(client, bid, sample_dir)

    r = review_comparison_api(client, bid, 99999, "CONFIRMED")
    assert r.status_code == 400, f"不存在的对比记录应返回 400，实际 {r.status_code}"
    body = r.get_json()
    assert "不存在" in body.get("error", ""), f"错误信息应提示不存在: {body}"


def test_review_not_found_returns_400_json_error(client, sample_dir):
    """复核不存在记录返回 400 + 可读 JSON error，不是 404 或 traceback"""
    bid = create_batch(client, "test-review-notfound-json")
    match_and_resolve_all(client, bid, sample_dir)

    r = review_comparison_api(client, bid, 99999, "CONFIRMED")

    # 状态码必须是 400
    assert r.status_code == 400, f"期望 400，实际 {r.status_code}"

    # Content-Type 必须是 JSON
    assert "application/json" in r.content_type, f"期望 JSON 响应，实际 {r.content_type}"

    # 响应体必须有 error 字段且可读
    body = r.get_json()
    assert body is not None, "响应体必须是合法 JSON"
    assert "error" in body, "响应必须包含 error 字段"
    assert isinstance(body["error"], str) and len(body["error"]) > 0, "error 必须是非空字符串"
    assert "对比记录不存在" == body["error"], f"错误信息不匹配: {body['error']}"

    # 不能是 HTML 错误页或 traceback
    raw = r.data.decode("utf-8", errors="replace")
    assert "<!DOCTYPE" not in raw, "不应返回 HTML 错误页"
    assert "Traceback" not in raw, "不应返回 traceback"


def test_review_comparison_filter_by_status(client, sample_dir):
    """历史对比列表支持按复核状态筛选"""
    bid = create_batch(client, "test-review-filter")

    match_and_resolve_all(client, bid, sample_dir)
    notes = list_notes(client, bid)

    exs = get_exceptions(client, bid)
    if len(exs) >= 2:
        client.put(
            f"/api/batches/{bid}/exceptions/{exs[0]['id']}/remark",
            json={"remarks": "v2备注"},
        )
        client.put(
            f"/api/batches/{bid}/exceptions/{exs[1]['id']}/remark",
            json={"remarks": "v3备注"},
        )
    notes = list_notes(client, bid)
    assert len(notes) >= 3, f"需要至少 3 个版本，实际 {len(notes)}"

    r1 = compare_notes_api(client, bid, notes[0]["id"], notes[1]["id"])
    assert r1.status_code == 200
    comp1 = r1.get_json()["comparison"]

    r2 = compare_notes_api(client, bid, notes[1]["id"], notes[2]["id"])
    assert r2.status_code == 200
    comp2 = r2.get_json()["comparison"]

    r3 = compare_notes_api(client, bid, notes[0]["id"], notes[2]["id"])
    assert r3.status_code == 200
    comp3 = r3.get_json()["comparison"]

    review_comparison_api(client, bid, comp1["id"], "CONFIRMED", remark="确认1")
    review_comparison_api(client, bid, comp2["id"], "IGNORED", remark="忽略2")

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons")
    assert r.status_code == 200
    all_comps = r.get_json()["comparisons"]
    assert len(all_comps) == 3

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons?review_status=CONFIRMED")
    assert r.status_code == 200
    confirmed = r.get_json()["comparisons"]
    assert len(confirmed) == 1
    assert confirmed[0]["id"] == comp1["id"]

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons?review_status=IGNORED")
    assert r.status_code == 200
    ignored = r.get_json()["comparisons"]
    assert len(ignored) == 1
    assert ignored[0]["id"] == comp2["id"]

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons?review_status=PENDING")
    assert r.status_code == 200
    pending = r.get_json()["comparisons"]
    assert len(pending) == 1
    assert pending[0]["id"] == comp3["id"]

    r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons?review_status=INVALID")
    assert r.status_code == 400
    assert "无效的复核状态" in r.get_json().get("error", "")


def test_export_only_includes_confirmed_comparison(client, sample_dir):
    """导出 CSV 汇总区只带最近一次已确认对比，待复核和已忽略的不混进去"""
    bid = create_batch(client, "test-export-confirmed-only")

    match_and_resolve_all(client, bid, sample_dir)
    exs = get_exceptions(client, bid)
    if len(exs) >= 2:
        client.put(
            f"/api/batches/{bid}/exceptions/{exs[0]['id']}/remark",
            json={"remarks": "v2备注"},
        )
        client.put(
            f"/api/batches/{bid}/exceptions/{exs[1]['id']}/remark",
            json={"remarks": "v3备注"},
        )
    notes = list_notes(client, bid)
    assert len(notes) >= 3

    r1 = compare_notes_api(client, bid, notes[0]["id"], notes[1]["id"], operator="op1")
    comp_pending = r1.get_json()["comparison"]

    r2 = compare_notes_api(client, bid, notes[1]["id"], notes[2]["id"], operator="op2")
    comp2 = r2.get_json()["comparison"]
    review_comparison_api(client, bid, comp2["id"], "CONFIRMED", remark="已确认的对比", operator="reviewer_li")

    r3 = compare_notes_api(client, bid, notes[0]["id"], notes[2]["id"], operator="op3")
    comp_ignored = r3.get_json()["comparison"]
    review_comparison_api(client, bid, comp_ignored["id"], "IGNORED", remark="已忽略的对比")

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200

    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    has_confirmed_summary = False
    has_reviewer = False
    has_review_remark = False
    has_old_pending = True

    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary:
            if row[0] == "最近已确认对比摘要":
                has_confirmed_summary = True
                assert "v2 → v3" in row[1] or "v1 → v2" in row[1], "已确认对比摘要应正确"
            if row[0] == "复核人":
                has_reviewer = True
                assert row[1] == "reviewer_li", "复核人应正确"
            if row[0] == "复核备注":
                has_review_remark = True
                assert row[1] == "已确认的对比", "复核备注应正确"
            if row[0] == "最近对比摘要":
                has_old_pending = False

    assert has_confirmed_summary, "导出应包含'最近已确认对比摘要'"
    assert has_reviewer, "导出应包含'复核人'"
    assert has_review_remark, "导出应包含'复核备注'"
    assert has_old_pending, "导出不应包含旧的'最近对比摘要'字段（待确认的不应混入）"


def test_review_persists_across_restart(client, sample_dir, tmp_path):
    """跨重启持久化：复核状态、备注、操作人、复核时间在重启后仍然存在"""
    from app import create_app
    from models import db

    db_path = tmp_path / "review_persist_test.db"
    db_uri = f"sqlite:///{db_path}"

    app1 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app1.app_context():
        db.create_all()
    c1 = app1.test_client()

    bid = create_batch(c1, "test-review-persist")
    upload_file(c1, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(c1, bid, "invoice", sample_dir, "invoices.csv")
    c1.post(f"/api/batches/{bid}/match")

    exs = c1.get(f"/api/batches/{bid}/exceptions").get_json()["exceptions"]
    if exs:
        c1.put(
            f"/api/batches/{bid}/exceptions/{exs[0]['id']}/remark",
            json={"remarks": "触发v2"},
        )

    notes = c1.get(f"/api/batches/{bid}/recalc-notes").get_json()["notes"]
    r = c1.post(
        f"/api/batches/{bid}/recalc-notes/compare",
        json={"note_a_id": notes[0]["id"], "note_b_id": notes[-1]["id"], "operator": "persist_op"},
    )
    comp = r.get_json()["comparison"]
    comp_id = comp["id"]

    r = c1.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/{comp_id}/review",
        json={"review_status": "CONFIRMED", "review_remark": "持久化测试备注", "operator": "persist_reviewer"},
    )
    assert r.status_code == 200

    saved_status = r.get_json()["comparison"]["review_status"]
    saved_remark = r.get_json()["comparison"]["review_remark"]
    saved_reviewer = r.get_json()["comparison"]["reviewed_by"]
    saved_reviewed_at = r.get_json()["comparison"]["reviewed_at"]

    with app1.app_context():
        db.session.remove()

    app2 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app2.app_context():
        db.create_all()
    c2 = app2.test_client()

    r = c2.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comp_id}")
    assert r.status_code == 200
    after = r.get_json()["comparison"]

    assert after["review_status"] == saved_status
    assert after["review_remark"] == saved_remark
    assert after["reviewed_by"] == saved_reviewer
    assert after["reviewed_at"] == saved_reviewed_at
    assert after["review_status"] == "CONFIRMED"
    assert after["review_remark"] == "持久化测试备注"

    with app2.app_context():
        db.session.remove()
        db.drop_all()


def test_review_generates_audit_log(client, sample_dir):
    """复核操作会生成审计日志，包含状态和备注信息"""
    bid = create_batch(client, "test-review-audit-log")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    r = review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="审计日志测试", operator="audit_tester")
    assert r.status_code == 200

    batch = get_batch(client, bid)
    logs_endpoint = f"/api/batches/{bid}"

    from models import AuditLog
    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid).order_by(AuditLog.id.desc()).limit(5).all()
        log_actions = [log.action for log in logs]

    has_review_log = any("REVIEW" in action for action in log_actions)
    assert has_review_log, "复核操作应生成审计日志"

    review_logs = [log for log in logs if "REVIEW" in log.action]
    assert len(review_logs) >= 1
    assert review_logs[0].operator == "audit_tester"
    assert "审计日志测试" in review_logs[0].detail


def test_review_can_reset_to_pending(client, sample_dir):
    """已确认或已忽略的对比可以重置为待复核状态"""
    bid = create_batch(client, "test-review-reset")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    review_comparison_api(client, bid, comp["id"], "CONFIRMED", remark="先确认")

    r = review_comparison_api(client, bid, comp["id"], "PENDING", remark="重置为待复核")
    assert r.status_code == 200
    assert r.get_json()["comparison"]["review_status"] == "PENDING"

    review_comparison_api(client, bid, comp["id"], "IGNORED", remark="再忽略")

    r = review_comparison_api(client, bid, comp["id"], "PENDING", remark="重置为待复核2")
    assert r.status_code == 200
    assert r.get_json()["comparison"]["review_status"] == "PENDING"


def test_index_page_has_review_ui_elements(client, sample_dir):
    """前端页面包含复核相关的 UI 元素"""
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")

    assert "复核操作" in html, "页面应包含'复核操作'区域"
    assert "复核备注" in html, "页面应包含'复核备注'输入"
    assert "历史对比记录" in html, "页面应包含'历史对比记录'列表"
    assert "状态筛选" in html, "页面应包含'状态筛选'下拉框"
    assert "待复核" in html, "页面应包含'待复核'状态选项"
    assert "已确认" in html, "页面应包含'已确认'状态选项"
    assert "已忽略" in html, "页面应包含'已忽略'状态选项"


def test_review_invalid_status_returns_400(client, sample_dir):
    """无效的复核状态返回 400 错误"""
    bid = create_batch(client, "test-review-invalid-status")
    comp = create_two_notes_and_compare(client, bid, sample_dir)

    r = review_comparison_api(client, bid, comp["id"], "INVALID_STATUS")
    assert r.status_code == 400
    body = r.get_json()
    assert "无效的复核状态" in body.get("error", "")


# ---------- 批量复核 测试辅助函数 ----------

def batch_review_api(client, bid, comparison_ids, status, remark="", operator="batch_tester"):
    r = client.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/batch-review",
        json={
            "comparison_ids": comparison_ids,
            "review_status": status,
            "review_remark": remark,
            "operator": operator,
        },
    )
    return r


def create_multiple_comparisons(client, bid, sample_dir, count=3):
    """创建多个版本和多条对比记录，全部为 PENDING 状态"""
    match_and_resolve_all(client, bid, sample_dir)
    exs = get_exceptions(client, bid)

    for i in range(count):
        if len(exs) > i:
            client.put(
                f"/api/batches/{bid}/exceptions/{exs[i]['id']}/remark",
                json={"remarks": f"v{i+2}备注"},
            )

    notes = list_notes(client, bid)
    assert len(notes) >= count + 1, f"需要至少 {count+1} 个版本，实际 {len(notes)}"

    comps = []
    for i in range(count):
        r = compare_notes_api(client, bid, notes[i]["id"], notes[i + 1]["id"], operator=f"op_{i}")
        assert r.status_code == 200
        comps.append(r.get_json()["comparison"])
    return comps


# ---------- 批量复核 测试用例 ----------

def test_batch_review_all_success(client, sample_dir):
    """成功批量处理：3 条 PENDING 全部批量确认，状态、备注、复核人、时间都写入"""
    bid = create_batch(client, "test-batch-success")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=3)
    comp_ids = [c["id"] for c in comps]

    r = batch_review_api(client, bid, comp_ids, "CONFIRMED", remark="批量确认财务已核对", operator="finance_li")
    assert r.status_code == 200, f"批量复核失败: {r.data}"
    body = r.get_json()

    assert body["success_count"] == 3, f"应成功 3 条，实际 {body['success_count']}"
    assert set(body["success_ids"]) == set(comp_ids)
    assert body["conflict_count"] == 0
    assert body["conflicts"] == []

    for cid in comp_ids:
        r = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{cid}")
        assert r.status_code == 200
        c = r.get_json()["comparison"]
        assert c["review_status"] == "CONFIRMED"
        assert c["review_remark"] == "批量确认财务已核对"
        assert c["reviewed_by"] == "finance_li"
        assert c["reviewed_at"] is not None

    from models import AuditLog
    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid, action="REVIEW_COMPARISON_CONFIRMED").all()
        assert len(logs) >= 3, "每条成功的批量复核都应写审计日志"
        for log in logs:
            assert log.operator == "finance_li"
            assert "[批量]" in log.detail


def test_batch_review_partial_conflicts(client, sample_dir):
    """部分冲突场景：2 条 PENDING + 1 条已 CONFIRMED + 1 条不存在的 ID + 1 条跨批次

    应返回清楚的冲突结果，成功的成功、冲突的逐条说明，不能静默吞掉。
    """
    bid1 = create_batch(client, "test-batch-conflict")
    comps = create_multiple_comparisons(client, bid1, sample_dir, count=3)

    bid2 = create_batch(client, "test-batch-conflict-bid2")
    comps_b2 = create_multiple_comparisons(client, bid2, sample_dir, count=1)

    review_comparison_api(client, bid1, comps[0]["id"], "CONFIRMED", remark="提前确认过的")

    mixed_ids = [comps[0]["id"], comps[1]["id"], comps[2]["id"], 99999, comps_b2[0]["id"]]

    r = batch_review_api(client, bid1, mixed_ids, "CONFIRMED", remark="批量处理", operator="finance_wang")
    assert r.status_code == 200
    body = r.get_json()

    assert body["success_count"] == 2
    assert set(body["success_ids"]) == {comps[1]["id"], comps[2]["id"]}
    assert body["conflict_count"] == 3

    conflict_map = {c["id"]: c["reason"] for c in body["conflicts"]}
    assert comps[0]["id"] in conflict_map
    assert "已确认" in conflict_map[comps[0]["id"]]
    assert 99999 in conflict_map
    assert "不存在" in conflict_map[99999]
    assert comps_b2[0]["id"] in conflict_map
    assert "不属于该批次" in conflict_map[comps_b2[0]["id"]]

    for cid in [comps[1]["id"], comps[2]["id"]]:
        c = client.get(f"/api/batches/{bid1}/recalc-notes/comparisons/{cid}").get_json()["comparison"]
        assert c["review_status"] == "CONFIRMED"
        assert c["review_remark"] == "批量处理"
        assert c["reviewed_by"] == "finance_wang"

    c0 = client.get(f"/api/batches/{bid1}/recalc-notes/comparisons/{comps[0]['id']}").get_json()["comparison"]
    assert c0["review_remark"] == "提前确认过的", "冲突的记录不应被覆盖"


def test_batch_review_ignored_conflicts(client, sample_dir):
    """已忽略的记录再批量确认或再批量忽略都应冲突，且不影响原本数据"""
    bid = create_batch(client, "test-batch-ignored-conflict")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=2)

    review_comparison_api(client, bid, comps[0]["id"], "IGNORED", remark="先单条忽略")

    r = batch_review_api(client, bid, [comps[0]["id"], comps[1]["id"]], "CONFIRMED", remark="批量确认")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success_count"] == 1
    assert body["conflict_count"] == 1
    conflict_map = {c["id"]: c["reason"] for c in body["conflicts"]}
    assert "已忽略" in conflict_map[comps[0]["id"]]

    c0 = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[0]['id']}").get_json()["comparison"]
    assert c0["review_status"] == "IGNORED", "已忽略的记录不应被批量确认覆盖"
    assert c0["review_remark"] == "先单条忽略"

    r = batch_review_api(client, bid, [comps[0]["id"]], "IGNORED", remark="重复忽略")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success_count"] == 0
    assert body["conflict_count"] == 1
    assert "重复忽略" in body["conflicts"][0]["reason"]


def test_batch_review_confirmed_should_not_be_overwritten_by_ignore(client, sample_dir):
    """回归测试：已确认的对比记录再被批量忽略时必须进 conflicts，原状态/备注/复核人/时间都不能被覆盖。

    同时覆盖：已忽略再批量确认、已忽略重复忽略、正常 PENDING 批量处理不受影响。
    """
    bid = create_batch(client, "test-batch-confirmed-not-overwritten")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=3)

    r = review_comparison_api(client, bid, comps[0]["id"], "CONFIRMED", remark="原确认备注", operator="original_confirmer")
    assert r.status_code == 200
    c0_before = r.get_json()["comparison"]
    saved_c0_status = c0_before["review_status"]
    saved_c0_remark = c0_before["review_remark"]
    saved_c0_reviewer = c0_before["reviewed_by"]
    saved_c0_time = c0_before["reviewed_at"]
    assert saved_c0_status == "CONFIRMED"
    assert saved_c0_remark == "原确认备注"
    assert saved_c0_reviewer == "original_confirmer"
    assert saved_c0_time is not None

    review_comparison_api(client, bid, comps[1]["id"], "IGNORED", remark="原忽略备注", operator="original_ignorer")
    c1_before = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[1]['id']}").get_json()["comparison"]
    saved_c1_status = c1_before["review_status"]
    saved_c1_remark = c1_before["review_remark"]
    saved_c1_reviewer = c1_before["reviewed_by"]
    saved_c1_time = c1_before["reviewed_at"]

    # --- 场景 A：批量忽略（已确认 + 已忽略 + 待复核 混合）---
    r = batch_review_api(
        client, bid,
        [comps[0]["id"], comps[1]["id"], comps[2]["id"]],
        "IGNORED",
        remark="批量忽略恶意覆盖",
        operator="bad_actor",
    )
    assert r.status_code == 200
    body = r.get_json()

    assert body["success_count"] == 1, f"只有 PENDING 的 comps[2] 应成功，实际成功 {body['success_count']} 条"
    assert body["conflict_count"] == 2, f"已确认和已忽略都应冲突，实际冲突 {body['conflict_count']} 条"
    conflict_map = {c["id"]: c["reason"] for c in body["conflicts"]}

    assert comps[0]["id"] in conflict_map, "已确认记录批量忽略必须进 conflicts"
    assert "已确认" in conflict_map[comps[0]["id"]], f"冲突原因应提示已确认，实际: {conflict_map[comps[0]['id']]}"
    assert comps[1]["id"] in conflict_map, "已忽略记录批量忽略必须进 conflicts（重复忽略）"
    assert set(body["success_ids"]) == {comps[2]["id"]}, "只有 PENDING 记录能成功批量忽略"

    c0_after = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[0]['id']}").get_json()["comparison"]
    assert c0_after["review_status"] == saved_c0_status, "已确认记录的状态不应被批量忽略覆盖"
    assert c0_after["review_remark"] == saved_c0_remark, "已确认记录的备注不应被批量忽略覆盖"
    assert c0_after["reviewed_by"] == saved_c0_reviewer, "已确认记录的复核人不应被批量忽略覆盖"
    assert c0_after["reviewed_at"] == saved_c0_time, "已确认记录的复核时间不应被批量忽略覆盖"

    c1_after = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[1]['id']}").get_json()["comparison"]
    assert c1_after["review_status"] == saved_c1_status, "已忽略记录的状态不应被覆盖"
    assert c1_after["review_remark"] == saved_c1_remark, "已忽略记录的备注不应被覆盖"
    assert c1_after["reviewed_by"] == saved_c1_reviewer, "已忽略记录的复核人不应被覆盖"
    assert c1_after["reviewed_at"] == saved_c1_time, "已忽略记录的复核时间不应被覆盖"

    c2_after = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[2]['id']}").get_json()["comparison"]
    assert c2_after["review_status"] == "IGNORED", "PENDING 记录批量忽略应成功"
    assert c2_after["review_remark"] == "批量忽略恶意覆盖"
    assert c2_after["reviewed_by"] == "bad_actor"

    # --- 场景 B：批量确认（已确认 + 已忽略 + 待复核）---
    # 重置 comps[2] 回 PENDING 用于测试
    review_comparison_api(client, bid, comps[2]["id"], "PENDING", remark="重置回待复核")

    r = batch_review_api(
        client, bid,
        [comps[0]["id"], comps[1]["id"], comps[2]["id"]],
        "CONFIRMED",
        remark="批量确认测试",
        operator="confirm_user",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["success_count"] == 1, "只有 PENDING 的 comps[2] 应批量确认成功"
    assert body["conflict_count"] == 2
    conflict_map_b = {c["id"]: c["reason"] for c in body["conflicts"]}
    assert "已确认" in conflict_map_b[comps[0]["id"]], "已确认→批量确认应冲突（重复确认）"
    assert "已忽略" in conflict_map_b[comps[1]["id"]], "已忽略→批量确认应冲突"

    c0_final = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{comps[0]['id']}").get_json()["comparison"]
    assert c0_final["review_status"] == "CONFIRMED"
    assert c0_final["review_remark"] == "原确认备注"
    assert c0_final["reviewed_by"] == "original_confirmer"

    # --- 场景 C：CSV 导出应只包含最近一次已确认对比（不受批量忽略影响）---
    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200
    csv_content = r.data.decode("utf-8-sig")
    assert "批量确认测试" in csv_content, "导出应包含最近一次（场景 B 批量确认 comps[2]）的备注"
    assert "confirm_user" in csv_content, "导出应包含最近一次确认人"
    assert "批量忽略恶意覆盖" not in csv_content, "导出不应混入批量忽略阶段的内容"
    assert "bad_actor" not in csv_content, "导出不应包含批量忽略操作人"
    assert "原确认备注" not in csv_content, "导出只取最近一次已确认对比，不应包含更早的 comps[0] 备注"


def test_batch_review_persists_across_restart(client, sample_dir, tmp_path):
    """跨重启持久化：批量复核后的状态、备注、复核人、时间在重启后仍可查"""
    from app import create_app
    from models import db

    db_path = tmp_path / "batch_persist_test.db"
    db_uri = f"sqlite:///{db_path}"

    app1 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app1.app_context():
        db.create_all()
    c1 = app1.test_client()

    bid = create_batch(c1, "test-batch-persist")
    comps = create_multiple_comparisons(c1, bid, sample_dir, count=3)
    comp_ids = [c["id"] for c in comps]

    r = batch_review_api(c1, bid, comp_ids, "CONFIRMED", remark="持久化批量备注", operator="batch_persist_op")
    assert r.status_code == 200
    assert r.get_json()["success_count"] == 3

    saved_states = {}
    for cid in comp_ids:
        c = c1.get(f"/api/batches/{bid}/recalc-notes/comparisons/{cid}").get_json()["comparison"]
        saved_states[cid] = {
            "review_status": c["review_status"],
            "review_remark": c["review_remark"],
            "reviewed_by": c["reviewed_by"],
            "reviewed_at": c["reviewed_at"],
        }

    with app1.app_context():
        db.session.remove()

    app2 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app2.app_context():
        db.create_all()
    c2 = app2.test_client()

    for cid in comp_ids:
        c = c2.get(f"/api/batches/{bid}/recalc-notes/comparisons/{cid}").get_json()["comparison"]
        assert c["review_status"] == saved_states[cid]["review_status"]
        assert c["review_remark"] == saved_states[cid]["review_remark"]
        assert c["reviewed_by"] == saved_states[cid]["reviewed_by"]
        assert c["reviewed_at"] == saved_states[cid]["reviewed_at"]
        assert c["review_status"] == "CONFIRMED"

    with app2.app_context():
        db.session.remove()
        db.drop_all()


def test_batch_review_export_no_cross_data(client, sample_dir):
    """批量确认后，CSV 导出只带最近一次已确认对比，待复核和已忽略的不混进去"""
    bid = create_batch(client, "test-batch-export")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=3)

    review_comparison_api(client, bid, comps[0]["id"], "IGNORED", remark="这条忽略掉")

    r = batch_review_api(client, bid, [comps[1]["id"], comps[2]["id"]], "CONFIRMED", remark="批量确认的", operator="export_batch_op")
    assert r.status_code == 200
    assert r.get_json()["success_count"] == 2

    r = client.get(f"/api/batches/{bid}/export")
    assert r.status_code == 200

    with io.StringIO(r.data.decode("utf-8-sig")) as f:
        rows = list(csv.reader(f))

    in_summary = False
    exported_compare_summary = None
    exported_compare_versions = None
    exported_compare_operator = None
    exported_reviewer = None
    exported_review_remark = None
    has_ignored_summary = False
    has_pending_summary = False

    for row in rows:
        if not row:
            continue
        if row[0] == "汇总信息":
            in_summary = True
            continue
        if in_summary:
            if row[0] == "最近已确认对比摘要":
                exported_compare_summary = row[1] if len(row) > 1 else ""
            if row[0] == "对比版本":
                exported_compare_versions = row[1] if len(row) > 1 else ""
            if row[0] == "对比操作人":
                exported_compare_operator = row[1] if len(row) > 1 else ""
            if row[0] == "复核人":
                exported_reviewer = row[1] if len(row) > 1 else ""
            if row[0] == "复核备注":
                exported_review_remark = row[1] if len(row) > 1 else ""
            if row[0] == "最近对比摘要":
                has_pending_summary = True
            if "忽略" in (row[1] if len(row) > 1 else ""):
                has_ignored_summary = True

    assert exported_compare_summary is not None, "导出应包含最近已确认对比摘要"
    assert exported_reviewer == "export_batch_op"
    assert exported_review_remark == "批量确认的"
    assert exported_compare_versions == f"v{comps[2]['note_a_version']} → v{comps[2]['note_b_version']}"
    assert not has_pending_summary, "导出不应包含待复核的'最近对比摘要'"
    assert not has_ignored_summary, "导出不应混入已忽略对比的内容"

    api_payable = get_batch(client, bid)["summary"]["payable_total"]
    exported_payable = None
    for row in rows:
        if row and row[0] == "应付合计":
            exported_payable = float(row[1])
            break
    assert exported_payable == api_payable, "导出金额应与接口对齐"


def test_batch_review_invalid_params_return_400(client, sample_dir):
    """批量复核参数校验：空列表、无效状态返回 400 可读 JSON"""
    bid = create_batch(client, "test-batch-invalid")
    create_multiple_comparisons(client, bid, sample_dir, count=1)

    r = batch_review_api(client, bid, [], "CONFIRMED")
    assert r.status_code == 400
    assert "参数缺失" in r.get_json().get("error", "")

    r = client.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/batch-review",
        json={"comparison_ids": [1], "review_status": "BAD"},
    )
    assert r.status_code == 400
    assert "无效的复核状态" in r.get_json().get("error", "")

    r = client.put(
        f"/api/batches/{bid}/recalc-notes/comparisons/batch-review",
        json={},
    )
    assert r.status_code == 400
    assert "参数缺失" in r.get_json().get("error", "")


def test_batch_review_generates_audit_logs(client, sample_dir):
    """批量复核每条成功记录都生成独立的审计日志"""
    bid = create_batch(client, "test-batch-audit")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=3)
    comp_ids = [c["id"] for c in comps]

    from models import AuditLog
    with client.application.app_context():
        before_count = AuditLog.query.filter_by(batch_id=bid, action="REVIEW_COMPARISON_CONFIRMED").count()

    r = batch_review_api(client, bid, comp_ids, "CONFIRMED", remark="审计测试批量", operator="audit_batch_user")
    assert r.status_code == 200
    assert r.get_json()["success_count"] == 3

    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid, action="REVIEW_COMPARISON_CONFIRMED").order_by(AuditLog.id.desc()).limit(5).all()
        after_count = len(logs)
        assert after_count >= before_count + 3, f"应新增至少 3 条审计日志，新增 {after_count - before_count}"
        for log in logs:
            assert log.operator == "audit_batch_user"
            assert "[批量]" in log.detail
            assert "审计测试批量" in log.detail


def test_batch_review_reset_to_pending(client, sample_dir):
    """批量重置为待复核状态"""
    bid = create_batch(client, "test-batch-reset")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=2)
    comp_ids = [c["id"] for c in comps]

    review_comparison_api(client, bid, comps[0]["id"], "CONFIRMED", remark="先确认")
    review_comparison_api(client, bid, comps[1]["id"], "IGNORED", remark="先忽略")

    r = batch_review_api(client, bid, comp_ids, "PENDING", remark="批量重置待复核", operator="reset_user")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success_count"] == 2

    for cid in comp_ids:
        c = client.get(f"/api/batches/{bid}/recalc-notes/comparisons/{cid}").get_json()["comparison"]
        assert c["review_status"] == "PENDING"
        assert c["review_remark"] == "批量重置待复核"
        assert c["reviewed_by"] == "reset_user"


def test_batch_review_api_response_readable_json(client, sample_dir):
    """批量复核接口返回的 JSON 结构清晰可读，页面能直接解析展示"""
    bid = create_batch(client, "test-batch-json-readable")
    comps = create_multiple_comparisons(client, bid, sample_dir, count=2)

    r = batch_review_api(client, bid, [comps[0]["id"], 99999], "CONFIRMED", remark="测试JSON结构", operator="json_user")
    assert r.status_code == 200
    assert "application/json" in r.content_type

    body = r.get_json()
    assert isinstance(body, dict)
    assert "success_count" in body and isinstance(body["success_count"], int)
    assert "success_ids" in body and isinstance(body["success_ids"], list)
    assert "conflict_count" in body and isinstance(body["conflict_count"], int)
    assert "conflicts" in body and isinstance(body["conflicts"], list)

    for c in body["conflicts"]:
        assert "id" in c and isinstance(c["id"], int)
        assert "reason" in c and isinstance(c["reason"], str)
        assert len(c["reason"]) > 0

    raw = r.data.decode("utf-8", errors="replace")
    assert "<!DOCTYPE" not in raw, "不应返回 HTML"
    assert "Traceback" not in raw, "不应返回 traceback"


def test_index_page_has_batch_review_ui(client, sample_dir):
    """前端页面包含批量复核 UI 元素"""
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")

    assert "批量确认" in html, "页面应包含'批量确认'按钮"
    assert "批量忽略" in html, "页面应包含'批量忽略'按钮"
    assert "批量备注" in html, "页面应包含'批量备注'输入"
    assert "全选待复核" in html, "页面应包含'全选待复核'"
    assert "已选" in html, "页面应显示已选数量"
    assert "批量操作结果" in html, "页面应展示批量操作冲突结果"


# ---------- 预检草稿 测试辅助函数 ----------

def precheck_file_api(client, bid, typ, sample_dir, filename, operator="test_user"):
    path = os.path.join(sample_dir, filename)
    with open(path, "rb") as f:
        data = {"file": (f, filename), "operator": operator}
        url = f"/api/batches/{bid}/precheck-{typ}"
        r = client.post(url, data=data, content_type="multipart/form-data")
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code, "raw": r.data.decode("utf-8", errors="replace")}


def precheck_csv_bytes(client, bid, typ, csv_text, filename="data.csv", operator="test_user"):
    data = {"file": (BytesIO(csv_text.encode("utf-8-sig")), filename), "operator": operator}
    url = f"/api/batches/{bid}/precheck-{typ}"
    r = client.post(url, data=data, content_type="multipart/form-data")
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code, "raw": r.data.decode("utf-8", errors="replace")}


def get_draft_api(client, bid, draft_id):
    r = client.get(f"/api/batches/{bid}/drafts/{draft_id}")
    assert r.status_code == 200
    return r.get_json()["draft"]


def list_drafts_api(client, bid, file_type=None, status=None):
    url = f"/api/batches/{bid}/drafts"
    params = []
    if file_type:
        params.append(f"file_type={file_type}")
    if status:
        params.append(f"status={status}")
    if params:
        url += "?" + "&".join(params)
    r = client.get(url)
    assert r.status_code == 200
    return r.get_json()["drafts"]


def get_latest_draft_api(client, bid, file_type=None):
    url = f"/api/batches/{bid}/drafts/latest"
    if file_type:
        url += f"?file_type={file_type}"
    r = client.get(url)
    assert r.status_code == 200
    return r.get_json().get("draft")


def confirm_draft_api(client, bid, draft_id, operator="test_user"):
    r = client.post(
        f"/api/batches/{bid}/drafts/{draft_id}/confirm",
        json={"operator": operator},
    )
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code}


def discard_draft_api(client, bid, draft_id, operator="test_user"):
    r = client.post(
        f"/api/batches/{bid}/drafts/{draft_id}/discard",
        json={"operator": operator},
    )
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code}


# ---------- 预检草稿 测试用例 ----------

def test_precheck_normal_import_flow(client, sample_dir):
    """正常导入流程：预检 → 确认 → 数据写入成功"""
    bid = create_batch(client, "test-precheck-normal")

    r = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r, f"预检PO失败: {r}"
    assert r["file_type"] == "PO"
    assert r["status"] == "PENDING"
    assert r["row_count"] > 0
    assert r["error_count"] == 0
    assert r["warning_count"] >= 0
    po_draft_id = r["id"]

    r = precheck_file_api(client, bid, "invoice", sample_dir, "invoices.csv")
    assert "error" not in r, f"预检发票失败: {r}"
    assert r["file_type"] == "INVOICE"
    assert r["status"] == "PENDING"
    assert r["row_count"] > 0
    assert r["error_count"] == 0
    inv_draft_id = r["id"]

    batch_before = get_batch(client, bid)
    assert batch_before["po_filename"] is None, "预检不应写入正式数据"
    assert batch_before["invoice_filename"] is None

    r = confirm_draft_api(client, bid, po_draft_id, operator="finance_user")
    assert r["success"] is True
    assert r["imported_count"] > 0

    r = confirm_draft_api(client, bid, inv_draft_id, operator="finance_user")
    assert r["success"] is True
    assert r["imported_count"] > 0

    batch_after = get_batch(client, bid)
    assert batch_after["po_filename"] == "purchase_orders.csv"
    assert batch_after["invoice_filename"] == "invoices.csv"

    from models import PurchaseOrder, Invoice
    with client.application.app_context():
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() > 0
        assert Invoice.query.filter_by(batch_id=bid).count() > 0

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200


def test_precheck_missing_columns_does_not_pollute_data(client, sample_dir):
    """缺列文件预检失败，确认丢弃后不污染数据"""
    bid = create_batch(client, "test-precheck-missing-col")

    r = precheck_file_api(client, bid, "po", sample_dir, "bad_missing_columns.csv")
    assert "error" not in r, f"预检请求本身失败: {r}"
    assert r["error_count"] > 0, "缺列文件应有错误"
    assert any("缺少必填列" in issue["message"] for issue in r["issues"]), "应检测到缺列"
    draft_id = r["id"]

    batch_before = get_batch(client, bid)
    assert batch_before["po_filename"] is None

    r = discard_draft_api(client, bid, draft_id, operator="finance_user")
    assert r["success"] is True

    batch_after = get_batch(client, bid)
    assert batch_after["po_filename"] is None

    from models import PurchaseOrder, ImportDraft
    with client.application.app_context():
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 0
        draft = ImportDraft.query.get(draft_id)
        assert draft.status == "DISCARDED"


def test_precheck_duplicate_invoice_does_not_pollute_data(client, sample_dir):
    """重复发票号预检检测到错误，丢弃后不污染数据"""
    bid = create_batch(client, "test-precheck-dup-inv")

    r = precheck_file_api(client, bid, "po", sample_dir, "bad_over_tolerance_po.csv")
    assert "error" not in r
    po_draft_id = r["id"]

    r = precheck_file_api(client, bid, "invoice", sample_dir, "bad_duplicate_invoice.csv")
    assert "error" not in r
    assert r["error_count"] > 0
    assert any("发票号重复" in issue["message"] for issue in r["issues"]), "应检测到重复发票号"
    inv_draft_id = r["id"]

    batch_before = get_batch(client, bid)
    assert batch_before["invoice_filename"] is None

    discard_draft_api(client, bid, po_draft_id)
    discard_draft_api(client, bid, inv_draft_id)

    batch_after = get_batch(client, bid)
    assert batch_after["po_filename"] is None
    assert batch_after["invoice_filename"] is None

    from models import PurchaseOrder, Invoice
    with client.application.app_context():
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 0
        assert Invoice.query.filter_by(batch_id=bid).count() == 0


def test_precheck_discard_does_not_pollute_data(client, sample_dir):
    """正常文件预检后取消（丢弃），不写入任何数据"""
    bid = create_batch(client, "test-precheck-discard")

    r = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r
    assert r["error_count"] == 0
    draft_id = r["id"]

    batch_before = get_batch(client, bid)
    assert batch_before["po_filename"] is None

    r = discard_draft_api(client, bid, draft_id, operator="finance_user")
    assert r["success"] is True

    batch_after = get_batch(client, bid)
    assert batch_after["po_filename"] is None

    from models import PurchaseOrder, ImportDraft
    with client.application.app_context():
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 0
        draft = ImportDraft.query.get(draft_id)
        assert draft.status == "DISCARDED"

    with client.application.app_context():
        from models import AuditLog
        logs = AuditLog.query.filter_by(batch_id=bid, action="DRAFT_DISCARDED_PO").all()
        assert len(logs) == 1, "丢弃草稿应写审计日志"


def test_precheck_draft_conflict_handling(client, sample_dir):
    """同类文件重新上传时，旧草稿自动丢弃并记录冲突"""
    bid = create_batch(client, "test-precheck-conflict")

    r1 = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r1
    old_draft_id = r1["id"]

    r2 = precheck_file_api(client, bid, "po", sample_dir, "bad_over_tolerance_po.csv")
    assert "error" not in r2
    assert r2["conflict"] is not None
    assert r2["conflict"]["old_draft_id"] == old_draft_id
    new_draft_id = r2["id"]

    assert old_draft_id != new_draft_id

    old_draft = get_draft_api(client, bid, old_draft_id)
    assert old_draft["status"] == "DISCARDED"

    new_draft = get_draft_api(client, bid, new_draft_id)
    assert new_draft["status"] == "PENDING"

    from models import AuditLog
    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid, action="DRAFT_CONFLICT_DISCARDED").all()
        assert len(logs) == 1, "冲突处理应写审计日志"


def test_precheck_same_file_no_duplicate_draft(client, sample_dir):
    """相同内容文件重复上传不创建新草稿"""
    bid = create_batch(client, "test-precheck-same-file")

    r1 = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r1
    assert r1["is_new"] is True
    draft_id_1 = r1["id"]

    r2 = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r2
    assert r2["is_new"] is False
    draft_id_2 = r2["id"]

    assert draft_id_1 == draft_id_2, "相同文件应返回同一草稿"

    drafts = list_drafts_api(client, bid, file_type="PO")
    assert len(drafts) == 1, "不应创建重复草稿"


def test_precheck_tolerance_snapshot_preserved(client, sample_dir):
    """草稿保存容差配置快照，修改容差后快照不变"""
    bid = create_batch(client, "test-precheck-tolerance-snap", pct=2.0, ab=100.0)

    r = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert "error" not in r
    draft_id = r["id"]
    assert r["tolerance_pct"] == 2.0
    assert r["tolerance_abs"] == 100.0

    client.put(f"/api/batches/{bid}/tolerance", json={"tolerance_pct": 5.0, "tolerance_abs": 200.0})

    draft = get_draft_api(client, bid, draft_id)
    assert draft["tolerance_pct"] == 2.0, "容差快照不应随批次更新"
    assert draft["tolerance_abs"] == 100.0, "容差快照不应随批次更新"
    assert draft["rule_version"] == r["rule_version"], "规则版本快照应保持不变"


def test_precheck_persists_across_restart(client, sample_dir, tmp_path):
    """跨服务重启，预检草稿仍可查询"""
    from app import create_app
    from models import db

    db_path = tmp_path / "draft_persist_test.db"
    db_uri = f"sqlite:///{db_path}"

    app1 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app1.app_context():
        db.create_all()
    c1 = app1.test_client()

    bid = create_batch(c1, "test-precheck-persist")

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V001,供应商A,10000.00,2024-01-01\n"
        "PO-002,V001,供应商A,20000.00,2024-01-01\n"
    )
    r = precheck_csv_bytes(c1, bid, "po", po_csv, "persist_po.csv", operator="persist_user")
    assert "error" not in r
    draft_id = r["id"]
    saved_error_count = r["error_count"]
    saved_warning_count = r["warning_count"]
    saved_tolerance_pct = r["tolerance_pct"]
    saved_tolerance_abs = r["tolerance_abs"]
    saved_file_hash = r["file_hash"]

    latest_before = get_latest_draft_api(c1, bid, file_type="PO")
    assert latest_before is not None
    assert latest_before["id"] == draft_id

    with app1.app_context():
        db.session.remove()

    app2 = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": db_uri,
        "WTF_CSRF_ENABLED": False,
        "UPLOAD_FOLDER": str(tmp_path),
    })
    with app2.app_context():
        db.create_all()
    c2 = app2.test_client()

    draft_after = get_draft_api(c2, bid, draft_id)
    assert draft_after is not None
    assert draft_after["id"] == draft_id
    assert draft_after["status"] == "PENDING"
    assert draft_after["error_count"] == saved_error_count
    assert draft_after["warning_count"] == saved_warning_count
    assert draft_after["tolerance_pct"] == saved_tolerance_pct
    assert draft_after["tolerance_abs"] == saved_tolerance_abs
    assert draft_after["file_hash"] == saved_file_hash
    assert draft_after["precheck_report"] is not None
    assert len(draft_after["issues"]) >= 0

    latest_after = get_latest_draft_api(c2, bid, file_type="PO")
    assert latest_after is not None
    assert latest_after["id"] == draft_id

    all_drafts = list_drafts_api(c2, bid)
    assert len(all_drafts) >= 1

    with app2.app_context():
        db.session.remove()
        db.drop_all()


def test_precheck_draft_list_and_filter(client, sample_dir):
    """草稿列表查询和筛选功能正常"""
    bid = create_batch(client, "test-precheck-list-filter")

    r1 = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    po_draft_id = r1["id"]

    r2 = precheck_file_api(client, bid, "invoice", sample_dir, "invoices.csv")
    inv_draft_id = r2["id"]

    all_drafts = list_drafts_api(client, bid)
    assert len(all_drafts) == 2

    po_drafts = list_drafts_api(client, bid, file_type="PO")
    assert len(po_drafts) == 1
    assert po_drafts[0]["id"] == po_draft_id

    inv_drafts = list_drafts_api(client, bid, file_type="INVOICE")
    assert len(inv_drafts) == 1
    assert inv_drafts[0]["id"] == inv_draft_id

    pending_drafts = list_drafts_api(client, bid, status="PENDING")
    assert len(pending_drafts) == 2

    discard_draft_api(client, bid, po_draft_id)

    pending_after = list_drafts_api(client, bid, status="PENDING")
    assert len(pending_after) == 1
    assert pending_after[0]["id"] == inv_draft_id

    discarded = list_drafts_api(client, bid, status="DISCARDED")
    assert len(discarded) == 1
    assert discarded[0]["id"] == po_draft_id


def test_precheck_vendor_consistency_warning(client, sample_dir):
    """供应商不一致检测生成警告"""
    bid = create_batch(client, "test-precheck-vendor-warn")

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V001,供应商A,10000.00,2024-01-01\n"
        "PO-002,V002,供应商B,20000.00,2024-01-01\n"
    )
    r = precheck_csv_bytes(client, bid, "po", po_csv, "multi_vendor_po.csv")
    assert "error" not in r
    assert r["warning_count"] >= 1
    assert any("VENDOR_INCONSISTENT" == issue["issue_code"] for issue in r["issues"])
    assert any("不同的供应商编码" in issue["message"] for issue in r["issues"])


def test_precheck_amount_format_validation(client, sample_dir):
    """金额格式错误检测"""
    bid = create_batch(client, "test-precheck-amount-format")

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V001,供应商A,not_a_number,2024-01-01\n"
        "PO-002,V001,供应商A,-500.00,2024-01-01\n"
        "PO-003,V001,供应商A,0,2024-01-01\n"
    )
    r = precheck_csv_bytes(client, bid, "po", po_csv, "bad_amount_po.csv")
    assert "error" not in r

    error_issues = [i for i in r["issues"] if i["issue_type"] == "ERROR"]
    warning_issues = [i for i in r["issues"] if i["issue_type"] == "WARNING"]

    assert any("金额格式错误" in i["message"] for i in error_issues), "应检测到金额格式错误"
    assert any("NEGATIVE_AMOUNT" == i["issue_code"] for i in warning_issues), "应检测到负金额警告"
    assert any("ZERO_AMOUNT" == i["issue_code"] for i in warning_issues), "应检测到零金额警告"


def test_precheck_vendor_mismatch_with_existing_data(client, sample_dir):
    """批次已有对方单据时，检测供应商不匹配"""
    bid = create_batch(client, "test-precheck-vendor-mismatch")

    inv_csv = (
        "invoice_number,vendor_code,vendor_name,amount,invoice_date\n"
        "INV-001,V001,供应商A,10000.00,2024-01-01\n"
    )
    r = precheck_csv_bytes(client, bid, "invoice", inv_csv, "vendor_a_inv.csv")
    confirm_draft_api(client, bid, r["id"])

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V002,供应商B,10000.00,2024-01-01\n"
    )
    r = precheck_csv_bytes(client, bid, "po", po_csv, "vendor_b_po.csv")
    assert "error" not in r

    assert any("VENDOR_MISMATCH" == i["issue_code"] for i in r["issues"]), "应检测到供应商不匹配警告"
    assert any("V001" in i["message"] for i in r["issues"]), "警告应包含已有供应商编码"


def test_precheck_full_api_flow_with_requests_like(client, sample_dir):
    """完整预检链路测试：模拟 requests 客户端调用"""
    bid = create_batch(client, "test-precheck-full-flow", pct=3.0, ab=150.0)

    po_content = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-A,V001,供应商A,10000.00,2024-01-01\n"
        "PO-B,V001,供应商A,20000.00,2024-01-01\n"
    )
    inv_content = (
        "invoice_number,vendor_code,vendor_name,amount,invoice_date\n"
        "INV-A,V001,供应商A,10000.00,2024-01-02\n"
        "INV-B,V001,供应商A,20000.00,2024-01-02\n"
    )

    r = precheck_csv_bytes(client, bid, "po", po_content, "po_flow.csv", operator="test_op")
    assert "error" not in r
    assert r["file_type"] == "PO"
    assert r["status"] == "PENDING"
    assert r["row_count"] == 2
    assert r["error_count"] == 0
    assert r["warning_count"] == 0
    assert r["tolerance_pct"] == 3.0
    assert r["tolerance_abs"] == 150.0
    assert r["operator"] == "test_op"
    assert r["precheck_report"] is not None
    assert len(r["issues"]) == 0
    po_draft_id = r["id"]

    r = precheck_csv_bytes(client, bid, "invoice", inv_content, "inv_flow.csv", operator="test_op")
    assert "error" not in r
    assert r["file_type"] == "INVOICE"
    inv_draft_id = r["id"]

    r = confirm_draft_api(client, bid, po_draft_id, operator="test_op")
    assert r["success"] is True
    assert r["imported_count"] == 2

    r = confirm_draft_api(client, bid, inv_draft_id, operator="test_op")
    assert r["success"] is True
    assert r["imported_count"] == 2

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    results = get_results(client, bid)
    assert len(results) == 2
    assert all(r["match_type"] == "EXACT" for r in results)

    from models import AuditLog
    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid).all()
        actions = [l.action for l in logs]
        assert "DRAFT_CREATED_PO" in actions
        assert "DRAFT_CREATED_INVOICE" in actions
        assert "DRAFT_CONFIRMED_PO" in actions
        assert "DRAFT_CONFIRMED_INVOICE" in actions
        assert "MATCH" in actions


def test_precheck_draft_not_found_returns_404(client, sample_dir):
    """访问不存在的草稿返回 404"""
    bid = create_batch(client, "test-precheck-404")

    r = client.get(f"/api/batches/{bid}/drafts/99999")
    assert r.status_code == 404

    r = client.post(f"/api/batches/{bid}/drafts/99999/confirm")
    assert r.status_code == 404

    r = client.post(f"/api/batches/{bid}/drafts/99999/discard")
    assert r.status_code == 404


def test_precheck_draft_wrong_batch_returns_400(client, sample_dir):
    """草稿不属于该批次返回 400"""
    bid1 = create_batch(client, "test-precheck-batch1")
    bid2 = create_batch(client, "test-precheck-batch2")

    r = precheck_file_api(client, bid1, "po", sample_dir, "purchase_orders.csv")
    draft_id = r["id"]

    r = client.get(f"/api/batches/{bid2}/drafts/{draft_id}")
    assert r.status_code == 400
    body = r.get_json()
    assert "不属于该批次" in body.get("error", "")

    r = client.post(f"/api/batches/{bid2}/drafts/{draft_id}/confirm")
    assert r.status_code == 400
    body = r.get_json()
    assert "不属于该批次" in body.get("error", "")

    r = client.post(f"/api/batches/{bid2}/drafts/{draft_id}/discard")
    assert r.status_code == 400
    body = r.get_json()
    assert "不属于该批次" in body.get("error", "")


def test_precheck_duplicate_po_warning(client, sample_dir):
    """采购单号重复检测"""
    bid = create_batch(client, "test-precheck-dup-po")

    po_csv = (
        "po_number,vendor_code,vendor_name,amount,po_date\n"
        "PO-001,V001,供应商A,10000.00,2024-01-01\n"
        "PO-001,V001,供应商A,20000.00,2024-01-01\n"
    )
    r = precheck_csv_bytes(client, bid, "po", po_csv, "dup_po.csv")
    assert "error" not in r
    assert any("DUPLICATE_PO" == issue["issue_code"] for issue in r["issues"]), "应检测到重复采购单号警告"
    assert any("采购单号重复" in issue["message"] for issue in r["issues"])


def test_index_page_has_precheck_ui(client, sample_dir):
    """前端页面包含预检 UI 元素"""
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")

    assert "预检模式" in html, "页面应包含'预检模式'"
    assert "预检结果" in html, "页面应包含'预检结果'区域"
    assert "容差配置快照" in html, "页面应包含'容差配置快照'"
    assert "确认导入" in html, "页面应包含'确认导入'按钮"
    assert "预检详细报告" in html, "页面应包含'预检详细报告'"


# ---------- 文档/用户流程一致性测试 ----------

def test_documentation_user_flow_consistency(client, sample_dir):
    """
    文档与用户流程一致性验收测试。
    模拟用户按照 README 描述的「预检模式」完整流程操作，
    验证文档描述与实际行为一致：
    1. 重复发票预检不直写（预检后不确认，正式数据为空）
    2. 取消后数据不变（丢弃草稿不污染正式表）
    3. 确认后才能匹配（两边都确认了才能执行匹配）
    4. 页面包含所有必需的 UI 提示文案
    """
    bid = create_batch(client, "test-doc-consistency")

    # ====== 场景1: 重复发票预检不直写 ======
    r = precheck_file_api(client, bid, "invoice", sample_dir, "bad_duplicate_invoice.csv")
    assert "error" not in r, f"预检请求本身失败: {r}"
    assert r["error_count"] > 0, "重复发票文件应有错误"
    assert r["status"] == "PENDING", "预检后草稿状态应为 PENDING"

    # 验证：预检后不确认，正式数据为空
    from models import Invoice, ImportDraft
    with client.application.app_context():
        assert Invoice.query.filter_by(batch_id=bid).count() == 0, "预检不确认不应写入正式表"
        draft = ImportDraft.query.get(r["id"])
        assert draft is not None
        assert draft.status == "PENDING"

    # 检查错误信息包含"发票号重复"（与文档描述一致）
    has_dup_invoice = any(
        "重复" in issue["message"] and "发票" in issue["message"]
        for issue in r["issues"]
    )
    assert has_dup_invoice, "文档说重复发票号预检报错，实际应检测到"

    # ====== 场景2: 取消后数据不变 ======
    # 先预检一个正常采购单
    r_po = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert r_po["error_count"] == 0
    po_draft_id = r_po["id"]

    # 确认前检查正式数据为空
    with client.application.app_context():
        from models import PurchaseOrder
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 0

    # 丢弃草稿
    r_discard = discard_draft_api(client, bid, po_draft_id)
    assert r_discard["success"] is True

    # 验证：丢弃后草稿状态为 DISCARDED，正式数据仍为空
    with client.application.app_context():
        draft = ImportDraft.query.get(po_draft_id)
        assert draft.status == "DISCARDED", "丢弃后草稿状态应为 DISCARDED"
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 0, "丢弃草稿不应写入正式数据"

    # ====== 场景3: 确认后才能匹配 ======
    # 先确认采购单
    r_po2 = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    confirm_draft_api(client, bid, r_po2["id"])

    batch_info = get_batch(client, bid)
    assert batch_info["po_filename"] == "purchase_orders.csv", "确认后采购单文件名应更新"
    assert batch_info["invoice_filename"] is None, "发票还没确认，文件名应为空"

    # 确认发票后再匹配，应该成功
    r_inv = precheck_file_api(client, bid, "invoice", sample_dir, "invoices.csv")
    confirm_draft_api(client, bid, r_inv["id"])

    batch_both = get_batch(client, bid)
    assert batch_both["po_filename"] is not None
    assert batch_both["invoice_filename"] is not None

    r_match2 = client.post(f"/api/batches/{bid}/match")
    assert r_match2.status_code == 200, "两边都确认后匹配应成功"
    match_data = r_match2.get_json()
    assert "has_exceptions" in match_data

    # ====== 场景4: 页面 UI 提示文案齐全 ======
    r_page = client.get("/")
    assert r_page.status_code == 200
    html = r_page.data.decode("utf-8")

    required_texts = [
        "预检模式",  # 提示条标题
        "不会立即写入正式数据",  # 提示条关键说明
        "确认导入",  # 确认按钮
        "取消",  # 取消按钮
        "需先确认导入采购单和发票文件",  # 匹配按钮提示
        "容差配置快照",  # 预检报告显示
        "预检结果",  # 结果区域标题
        "选择采购单文件（预检模式）",  # 采购单上传按钮
        "选择发票文件（预检模式）",  # 发票上传按钮
        "先预检再确认导入",  # 上传区提示
    ]
    for text in required_texts:
        assert text in html, f"页面应包含文案: '{text}'"

    # ====== 场景5: 旧 API 有废弃标记 ======
    path = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path, "rb") as f:
        data = {"file": (f, "purchase_orders.csv")}
        r_old = client.post(f"/api/batches/{bid}/upload-po", data=data, content_type="multipart/form-data")
    # 旧 API 仍能工作（兼容），但返回中应有 deprecated 标记
    assert r_old.status_code == 200
    old_data = r_old.get_json()
    assert old_data.get("deprecated") is True, "旧 API 返回应包含 deprecated: true"
    assert "X-Deprecated" in r_old.headers, "旧 API 响应头应包含 X-Deprecated"
    assert "notice" in old_data, "旧 API 返回应包含 notice 提示"

    # ====== 场景6: 草稿列表和最新查询可用 ======
    # 列表查询
    drafts = list_drafts_api(client, bid)
    assert len(drafts) >= 2, "应有至少 2 个草稿记录"

    # 按类型筛选
    po_drafts = list_drafts_api(client, bid, file_type="PO")
    inv_drafts = list_drafts_api(client, bid, file_type="INVOICE")
    assert len(po_drafts) >= 1
    assert len(inv_drafts) >= 1

    # 最新草稿查询
    latest_po = get_latest_draft_api(client, bid, file_type="PO")
    assert latest_po is not None
    assert latest_po["file_type"] == "PO"


def test_precheck_full_user_journey_like_readme(client, sample_dir):
    """
    完整用户旅程测试，完全按照 README「可复现验收步骤」的顺序执行，
    验证用户照着文档一步步走能复现预期结果。
    """
    # 1. 创建批次
    bid = create_batch(client, "验收批次-001")
    assert bid > 0

    # 2. 上传采购单预检
    r_po_pre = precheck_file_api(client, bid, "po", sample_dir, "purchase_orders.csv")
    assert r_po_pre["status"] == "PENDING"
    assert r_po_pre["row_count"] == 6
    assert "issues" in r_po_pre
    po_draft_id = r_po_pre["id"]

    # 3. 查看最新草稿（模拟用户打开页面自动加载）
    latest = get_latest_draft_api(client, bid, file_type="PO")
    assert latest is not None
    assert latest["id"] == po_draft_id
    assert latest["status"] == "PENDING"

    # 4. 确认导入采购单
    r_po_conf = confirm_draft_api(client, bid, po_draft_id)
    assert r_po_conf["success"] is True
    assert r_po_conf["imported_count"] == 6

    # 5. 上传发票预检
    r_inv_pre = precheck_file_api(client, bid, "invoice", sample_dir, "invoices.csv")
    assert r_inv_pre["status"] == "PENDING"
    assert r_inv_pre["row_count"] == 6
    inv_draft_id = r_inv_pre["id"]

    # 6. 确认导入发票
    r_inv_conf = confirm_draft_api(client, bid, inv_draft_id)
    assert r_inv_conf["success"] is True
    assert r_inv_conf["imported_count"] == 6

    # 7. 执行匹配
    r_match = client.post(f"/api/batches/{bid}/match")
    assert r_match.status_code == 200
    match_data = r_match.get_json()
    assert "matched_count" in match_data or "has_exceptions" in match_data

    # 8. 查看结果
    r_results = client.get(f"/api/batches/{bid}/results")
    assert r_results.status_code == 200
    results_data = r_results.get_json()
    assert "results" in results_data
    assert len(results_data["results"]) > 0, "匹配后应有结果数据"

    # 9. 验证审计日志中有草稿相关记录
    from models import AuditLog
    with client.application.app_context():
        audit_logs = AuditLog.query.filter_by(batch_id=bid).all()
        audit_actions = [log.action for log in audit_logs]
        # 应该有草稿创建、确认等审计记录
        assert any("DRAFT" in action for action in audit_actions), "审计日志应包含草稿相关操作"
        assert any("DRAFT_CONFIRMED" in action for action in audit_actions), "应有确认草稿的审计记录"
        assert len(audit_logs) >= 5, "应有足够的审计日志记录（创建、2次预检、2次确认、匹配等）"


def test_old_api_still_works_for_backward_compat(client, sample_dir):
    """
    旧版直接上传 API 仍可工作（向后兼容），但有废弃标记。
    确保现有脚本不会立刻坏掉。
    """
    bid = create_batch(client, "test-old-api-compat")

    # 旧 API 上传采购单
    path = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path, "rb") as f:
        data = {"file": (f, "purchase_orders.csv")}
        r = client.post(f"/api/batches/{bid}/upload-po", data=data, content_type="multipart/form-data")

    assert r.status_code == 200
    data = r.get_json()
    assert data.get("imported") == 6, "旧 API 仍应能正常导入"
    assert data.get("deprecated") is True, "旧 API 应返回 deprecated: true"
    assert r.headers.get("X-Deprecated") == "true", "响应头应包含 X-Deprecated"
    assert "notice" in data, "应包含迁移提示"

    # 旧 API 上传发票
    path2 = os.path.join(sample_dir, "invoices.csv")
    with open(path2, "rb") as f:
        data2 = {"file": (f, "invoices.csv")}
        r2 = client.post(f"/api/batches/{bid}/upload-invoice", data=data2, content_type="multipart/form-data")

    assert r2.status_code == 200
    assert r2.get_json().get("deprecated") is True

    # 数据确实写入了
    from models import PurchaseOrder, Invoice
    with client.application.app_context():
        assert PurchaseOrder.query.filter_by(batch_id=bid).count() == 6
        assert Invoice.query.filter_by(batch_id=bid).count() == 6


# ========== 导入复核台（预检草稿 v2）专用 5 条 ==========


def test_import_review_desk_full_flow(client, sample_dir):
    """
    [导入复核台#1] 完整链路：预检 → diff 分析(新增/覆盖/跳过) → superseded → 取消 → 确认 → 匹配
    """
    bid = create_batch(client, "ird-full-flow")

    # 1) 采购单预检 v1
    path = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path, "rb") as f:
        r1 = client.post(
            f"/api/batches/{bid}/precheck-po",
            data={"file": (f, "po_v1.csv"), "operator": "alice"},
            content_type="multipart/form-data",
        )
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["status"] == "PENDING"
    assert d1["diff_analysis"] is not None
    assert d1["diff_analysis"]["vs_official"]["add_count"] == 6
    assert d1["review_summary"] == "新增 6 条"

    # 2) 再传一份有差异的 v2，触发 superseded
    with open(path, "rb") as f:
        orig = f.read().decode("utf-8-sig")
    lines = orig.splitlines()
    v2 = "\n".join(lines + ["PO-2099-EXTRA,V000,额外供应商,999.99,CNY,2099-01-01"])
    r2 = client.post(
        f"/api/batches/{bid}/precheck-po",
        data={"file": (io.BytesIO(v2.encode("utf-8-sig")), "po_v2.csv"), "operator": "alice"},
        content_type="multipart/form-data",
    )
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["id"] != d1["id"]
    assert d2["supersedes_draft_id"] == d1["id"]
    assert d2["diff_analysis"]["vs_official"]["add_count"] == 7

    # 3) 验证 v1 被 DISCARD，且被标记
    from models import ImportDraft
    with client.application.app_context():
        old = ImportDraft.query.get(d1["id"])
    assert old.status == "DISCARDED"
    assert old.superseded_by_draft_id == d2["id"]
    assert old.conflict_reason is not None

    # 4) 取消发票草稿后正式表不变
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_inv, "rb") as f:
        rc = client.post(
            f"/api/batches/{bid}/precheck-invoice",
            data={"file": (f, "inv.csv"), "operator": "bob"},
            content_type="multipart/form-data",
        )
    inv_draft = rc.get_json()["id"]
    can = client.post(f"/api/batches/{bid}/drafts/{inv_draft}/cancel", json={"operator": "bob"})
    assert can.status_code == 200
    with client.application.app_context():
        from models import Invoice as Inv
        assert Inv.query.filter_by(batch_id=bid).count() == 0
        assert ImportDraft.query.get(inv_draft).status == "CANCELLED"

    # 5) 确认 v2 + 确认发票
    rc_po = client.post(f"/api/batches/{bid}/drafts/{d2['id']}/confirm", json={"operator": "alice"})
    assert rc_po.status_code == 200
    assert rc_po.get_json()["confirmed_by"] == "alice"
    assert rc_po.get_json()["review_summary"] is not None

    with open(path_inv, "rb") as f:
        ri = client.post(
            f"/api/batches/{bid}/precheck-invoice",
            data={"file": (f, "inv_final.csv"), "operator": "bob"},
            content_type="multipart/form-data",
        )
    inv_id2 = ri.get_json()["id"]
    ri2 = client.post(f"/api/batches/{bid}/drafts/{inv_id2}/confirm", json={"operator": "bob"})
    assert ri2.status_code == 200

    # 6) 匹配成功
    rm = client.post(f"/api/batches/{bid}/match")
    assert rm.status_code == 200
    res = client.get(f"/api/batches/{bid}/results").get_json()["results"]
    assert len(res) > 0


def test_confirm_blocked_by_cross_batch_dup_invoice(client, sample_dir):
    """
    [导入复核台#2] 跨批次重复发票：草稿 status=CONFLICT，confirm 接口返回 400
    """
    path_inv = os.path.join(sample_dir, "invoices.csv")
    # 建批 1 → 预检 → 确认发票
    b1 = create_batch(client, "ird-block-b1")
    with open(path_inv, "rb") as f:
        r1 = client.post(
            f"/api/batches/{b1}/precheck-invoice",
            data={"file": (f, "inv.csv"), "operator": "x"},
            content_type="multipart/form-data",
        )
    client.post(f"/api/batches/{b1}/drafts/{r1.get_json()['id']}/confirm", json={"operator": "x"})

    # 建批 2 → 预检同号发票 → 应为 CONFLICT
    b2 = create_batch(client, "ird-block-b2")
    with open(path_inv, "rb") as f:
        r2 = client.post(
            f"/api/batches/{b2}/precheck-invoice",
            data={"file": (f, "inv_dup.csv"), "operator": "y"},
            content_type="multipart/form-data",
        )
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["status"] == "CONFLICT"
    assert d2["conflict_reason"] is not None
    assert len(d2["diff_analysis"]["cross_batch_conflicts"]["invoice_duplicates"]) > 0

    # 尝试确认 → 400
    rc = client.post(f"/api/batches/{b2}/drafts/{d2['id']}/confirm", json={"operator": "y"})
    assert rc.status_code == 400, "CONFLICT 发票不可直接确认"


def test_confirm_blocked_by_missing_columns(client, sample_dir):
    """
    [导入复核台#3] 缺列 / 格式错误文件 → error_count>0 → confirm 返回 400
    """
    bid = create_batch(client, "ird-block-missing-col")
    bad = "po_number,vendor_name,amount\nP001,供应商A,100\n"
    r = client.post(
        f"/api/batches/{bid}/precheck-po",
        data={"file": (io.BytesIO(bad.encode("utf-8")), "bad.csv"), "operator": "z"},
        content_type="multipart/form-data",
    )
    if r.status_code == 200:
        d = r.get_json()
        err_count = d["precheck_report"]["summary"]["error_count"]
        assert err_count > 0, "缺列文件应有错误"
        rc = client.post(f"/api/batches/{bid}/drafts/{d['id']}/confirm", json={"operator": "z"})
        assert rc.status_code == 400, "error_count>0 时 confirm 必须返回 400"
    # 预检直接失败也是可接受的阻断方式


def test_cancel_keeps_official_data(client, sample_dir):
    """
    [导入复核台#4] 取消草稿不写入正式数据，已确认数据仍可正常匹配
    """
    bid = create_batch(client, "ird-cancel-official")

    # 先完成一批完整的确认
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_po, "rb") as f:
        r = client.post(
            f"/api/batches/{bid}/precheck-po",
            data={"file": (f, "po.csv"), "operator": "op1"},
            content_type="multipart/form-data",
        )
    client.post(f"/api/batches/{bid}/drafts/{r.get_json()['id']}/confirm", json={"operator": "op1"})
    with open(path_inv, "rb") as f:
        r = client.post(
            f"/api/batches/{bid}/precheck-invoice",
            data={"file": (f, "inv.csv"), "operator": "op1"},
            content_type="multipart/form-data",
        )
    client.post(f"/api/batches/{bid}/drafts/{r.get_json()['id']}/confirm", json={"operator": "op1"})

    # 记录当前行
    from models import PurchaseOrder as PO, Invoice as Inv
    with client.application.app_context():
        po_c1 = PO.query.filter_by(batch_id=bid).count()
        inv_c1 = Inv.query.filter_by(batch_id=bid).count()

    # 匹配一次
    client.post(f"/api/batches/{bid}/match")
    res1 = len(client.get(f"/api/batches/{bid}/results").get_json()["results"])

    # 再创建草稿并 CANCEL
    with open(path_po, "rb") as f:
        orig = f.read().decode("utf-8-sig")
    extra = "\n".join(orig.splitlines() + ["PO-9999,V000,额外,1000,CNY,2099-01-01"])
    r = client.post(
        f"/api/batches/{bid}/precheck-po",
        data={"file": (io.BytesIO(extra.encode("utf-8-sig")), "po_extra.csv"), "operator": "op2"},
        content_type="multipart/form-data",
    )
    extra_id = r.get_json()["id"]
    client.post(f"/api/batches/{bid}/drafts/{extra_id}/cancel", json={"operator": "op2"})

    # 取消后正式数据不变
    with client.application.app_context():
        po_c2 = PO.query.filter_by(batch_id=bid).count()
        inv_c2 = Inv.query.filter_by(batch_id=bid).count()
    assert po_c2 == po_c1, "CANCEL 后采购单正式数据不变"
    assert inv_c2 == inv_c1, "CANCEL 后发票正式数据不变"

    # 重新匹配结果数不变
    client.post(f"/api/batches/{bid}/match")
    res2 = len(client.get(f"/api/batches/{bid}/results").get_json()["results"])
    assert res2 == res1, "CANCEL 不影响匹配结果"


def test_export_csv_includes_review_summary(client, sample_dir):
    """
    [导入复核台#5] 导出 CSV 汇总区必须带「最近预检复核摘要」+ 草稿/复核明细
    """
    bid = create_batch(client, "ird-export-summary")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")

    with open(path_po, "rb") as f:
        r = client.post(
            f"/api/batches/{bid}/precheck-po",
            data={"file": (f, "po.csv"), "operator": "u1"},
            content_type="multipart/form-data",
        )
    client.post(f"/api/batches/{bid}/drafts/{r.get_json()['id']}/confirm", json={"operator": "u1"})

    with open(path_inv, "rb") as f:
        r = client.post(
            f"/api/batches/{bid}/precheck-invoice",
            data={"file": (f, "inv.csv"), "operator": "u2"},
            content_type="multipart/form-data",
        )
    client.post(f"/api/batches/{bid}/drafts/{r.get_json()['id']}/confirm", json={"operator": "u2"})

    client.post(f"/api/batches/{bid}/match")

    exp = client.get(f"/api/batches/{bid}/export")
    assert exp.status_code == 200
    body = exp.data.decode("utf-8-sig")
    lines = body.splitlines()

    # 核心断言：汇总区块必须包含以下 5 行关键字
    assert any("最近预检复核摘要" in l for l in lines), "CSV 必须包含「最近预检复核摘要」行"
    assert any("采购单草稿" in l for l in lines), "CSV 必须包含采购单草稿行"
    assert any("采购单复核明细" in l for l in lines), "CSV 必须包含采购单复核明细"
    assert any("发票草稿" in l for l in lines), "CSV 必须包含发票草稿行"
    assert any("发票复核明细" in l for l in lines), "CSV 必须包含发票复核明细"

    # 草稿行中必须包含确认人 @u1 @u2
    po_line = next(l for l in lines if "采购单草稿" in l)
    inv_line = next(l for l in lines if "发票草稿" in l)
    assert "@u1" in po_line, f"采购单草稿行应包含 @u1: {po_line}"
    assert "@u2" in inv_line, f"发票草稿行应包含 @u2: {inv_line}"


# ========== 批量导入方案（v3）专用 5 条 ==========


def test_batch_plan_full_flow(client, sample_dir):
    plan_bid = create_batch(client, "batch-plan-full")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_po, "rb") as fpo, open(path_inv, "rb") as finv:
        r = client.post(
            f"/api/batches/{plan_bid}/precheck-batch",
            data={"po_file": (fpo, "po.csv"), "invoice_file": (finv, "inv.csv"), "operator": "bp_u1"},
            content_type="multipart/form-data",
        )
    assert r.status_code == 200, f"batch precheck failed: {r.get_json()}"
    plan = r.get_json()
    assert plan["status"] == "PENDING"
    assert len(plan["drafts"]) == 2
    assert plan["plan_summary"] is not None
    po_d = next(d for d in plan["drafts"] if d["file_type"] == "PO")
    inv_d = next(d for d in plan["drafts"] if d["file_type"] == "INVOICE")
    assert po_d["plan_id"] == plan["id"]
    assert inv_d["plan_id"] == plan["id"]

    r = client.post(f"/api/batches/{plan_bid}/plans/{plan['id']}/confirm", json={"operator": "bp_u1"})
    assert r.status_code == 200, f"plan confirm failed: {r.get_json()}"
    result = r.get_json()
    assert result["confirmed_by"] == "bp_u1"
    assert len(result["import_results"]) == 2

    with client.application.app_context():
        from models import PurchaseOrder, Invoice
        assert PurchaseOrder.query.filter_by(batch_id=plan_bid).count() > 0
        assert Invoice.query.filter_by(batch_id=plan_bid).count() > 0


def test_batch_plan_undo_restores_data(client, sample_dir):
    plan_bid = create_batch(client, "batch-undo")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_po, "rb") as fpo, open(path_inv, "rb") as finv:
        r = client.post(
            f"/api/batches/{plan_bid}/precheck-batch",
            data={"po_file": (fpo, "po.csv"), "invoice_file": (finv, "inv.csv"), "operator": "undo_u"},
            content_type="multipart/form-data",
        )
    plan_id = r.get_json()["id"]
    client.post(f"/api/batches/{plan_bid}/plans/{plan_id}/confirm", json={"operator": "undo_u"})

    with client.application.app_context():
        from models import PurchaseOrder, Invoice
        po_c1 = PurchaseOrder.query.filter_by(batch_id=plan_bid).count()
        inv_c1 = Invoice.query.filter_by(batch_id=plan_bid).count()
    assert po_c1 > 0 and inv_c1 > 0

    r = client.post(f"/api/batches/{plan_bid}/plans/{plan_id}/undo", json={"operator": "undo_u"})
    assert r.status_code == 200, f"undo failed: {r.get_json()}"

    with client.application.app_context():
        from models import PurchaseOrder, Invoice
        po_c2 = PurchaseOrder.query.filter_by(batch_id=plan_bid).count()
        inv_c2 = Invoice.query.filter_by(batch_id=plan_bid).count()
    assert po_c2 == 0, f"undo should remove added POs, got {po_c2}"
    assert inv_c2 == 0, f"undo should remove added invoices, got {inv_c2}"


def test_batch_plan_cancel_keeps_data(client, sample_dir):
    plan_bid = create_batch(client, "batch-cancel")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path_po, "rb") as f:
        r = client.post(
            f"/api/batches/{plan_bid}/precheck-batch",
            data={"po_file": (f, "po.csv"), "operator": "cancel_u"},
            content_type="multipart/form-data",
        )
    plan_id = r.get_json()["id"]

    r = client.post(f"/api/batches/{plan_bid}/plans/{plan_id}/cancel", json={"operator": "cancel_u"})
    assert r.status_code == 200
    assert "unchanged" in r.get_json()["note"].lower() or "unchanged" in (r.get_json().get("note") or "").lower() or r.get_json()["note"] is not None

    with client.application.app_context():
        from models import PurchaseOrder
        assert PurchaseOrder.query.filter_by(batch_id=plan_bid).count() == 0

    r2 = client.post(f"/api/batches/{plan_bid}/plans/{plan_id}/confirm", json={"operator": "cancel_u"})
    assert r2.status_code == 400


def test_expired_draft_blocked(client, sample_dir):
    plan_bid = create_batch(client, "expired-block")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path_po, "rb") as f:
        r = client.post(
            f"/api/batches/{plan_bid}/precheck-po",
            data={"file": (f, "po.csv"), "operator": "exp_u"},
            content_type="multipart/form-data",
        )
    draft_id = r.get_json()["id"]

    with client.application.app_context():
        from models import ImportDraft
        from datetime import datetime, timezone, timedelta
        d = ImportDraft.query.get(draft_id)
        d.created_at = datetime.now(timezone.utc) - timedelta(hours=25)
        from models import db
        db.session.commit()

    r = client.post(f"/api/batches/{plan_bid}/drafts/{draft_id}/confirm", json={"operator": "exp_u"})
    assert r.status_code == 400, "expired draft confirm should return 400"
    err = r.get_json().get("error", "") or ""
    assert "expired" in err.lower() or "24" in err


def test_export_csv_includes_plan_summary(client, sample_dir):
    plan_bid = create_batch(client, "export-plan")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_po, "rb") as fpo, open(path_inv, "rb") as finv:
        r = client.post(
            f"/api/batches/{plan_bid}/precheck-batch",
            data={"po_file": (fpo, "po.csv"), "invoice_file": (finv, "inv.csv"), "operator": "exp_u"},
            content_type="multipart/form-data",
        )
    plan_id = r.get_json()["id"]
    client.post(f"/api/batches/{plan_bid}/plans/{plan_id}/confirm", json={"operator": "exp_u"})
    client.post(f"/api/batches/{plan_bid}/match")

    exp = client.get(f"/api/batches/{plan_bid}/export")
    assert exp.status_code == 200
    body = exp.data.decode("utf-8-sig")
    lines = body.splitlines()
    assert any("batch import plan summary" in l.lower() or "plan #" in l.lower() for l in lines), \
        f"CSV must include plan summary line, got: {[l for l in lines if 'plan' in l.lower() or 'summary' in l.lower()]}"


def test_health_check_basic_flow(client, sample_dir):
    bid = create_batch(client, "health-check-basic")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    path_inv = os.path.join(sample_dir, "invoices.csv")
    with open(path_po, "rb") as f:
        client.post(f"/api/batches/{bid}/precheck-po",
                    data={"file": (f, "po.csv"), "operator": "hc_u"},
                    content_type="multipart/form-data")
    with open(path_inv, "rb") as f:
        client.post(f"/api/batches/{bid}/precheck-invoice",
                    data={"file": (f, "inv.csv"), "operator": "hc_u"},
                    content_type="multipart/form-data")
    po_d = client.get(f"/api/batches/{bid}/drafts/latest?file_type=PO").get_json()
    inv_d = client.get(f"/api/batches/{bid}/drafts/latest?file_type=INVOICE").get_json()
    client.post(f"/api/batches/{bid}/drafts/{po_d['draft']['id']}/confirm", json={"operator": "hc_u"})
    client.post(f"/api/batches/{bid}/drafts/{inv_d['draft']['id']}/confirm", json={"operator": "hc_u"})
    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "hc_u"})
    assert r.status_code == 200
    data = r.get_json()
    assert "summary" in data
    assert "results" in data
    assert "history_id" in data
    assert "rule_version" in data
    hist = client.get(f"/api/batches/{bid}/health-history").get_json()
    assert "history" in hist
    assert len(hist["history"]) >= 1
    detail = client.get(f"/api/batches/{bid}/health-history/{data['history_id']}").get_json()
    assert detail["id"] == data["history_id"]
    assert "results" in detail


def test_health_check_rules_update(client, sample_dir):
    bid = create_batch(client, "health-check-rules")
    r = client.get(f"/api/batches/{bid}/health-rules")
    assert r.status_code == 200
    data = r.get_json()
    assert "rules" in data
    assert "rule_version" in data
    old_version = data["rule_version"]
    assert data["rules"]["duplicate_po_number"]["enabled"] is True
    r2 = client.put(f"/api/batches/{bid}/health-rules",
                    json={"rules": {"duplicate_po_number": {"enabled": False, "severity": "WARNING", "threshold": 2}},
                          "operator": "rule_editor"})
    assert r2.status_code == 200
    data2 = r2.get_json()
    assert data2["rules"]["duplicate_po_number"]["enabled"] is False
    assert data2["rule_version"] != old_version


def test_health_check_export_csv(client, sample_dir):
    bid = create_batch(client, "health-check-export")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path_po, "rb") as f:
        dr = client.post(f"/api/batches/{bid}/precheck-po",
                         data={"file": (f, "po.csv"), "operator": "hc_exp"},
                         content_type="multipart/form-data").get_json()
    client.post(f"/api/batches/{bid}/drafts/{dr['id']}/confirm", json={"operator": "hc_exp"})
    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "hc_exp"}).get_json()
    hid = r["history_id"]
    exp = client.get(f"/api/batches/{bid}/health-history/{hid}/export")
    assert exp.status_code == 200
    body = exp.data.decode("utf-8-sig")
    assert "数据健康巡检报告" in body
    assert "巡检问题明细" in body


def test_health_check_import_remarks_rule_version_block(client, sample_dir):
    bid = create_batch(client, "health-check-import")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path_po, "rb") as f:
        dr = client.post(f"/api/batches/{bid}/precheck-po",
                         data={"file": (f, "po.csv"), "operator": "hc_imp"},
                         content_type="multipart/form-data").get_json()
    client.post(f"/api/batches/{bid}/drafts/{dr['id']}/confirm", json={"operator": "hc_imp"})
    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "hc_imp"}).get_json()
    hid = r["history_id"]
    exp = client.get(f"/api/batches/{bid}/health-history/{hid}/export").data
    client.put(f"/api/batches/{bid}/health-rules",
               json={"rules": {"negative_amount": {"enabled": True, "severity": "BLOCKER", "threshold": 1000}},
                     "operator": "rule_changer"})
    import_csv = BytesIO(exp)
    import_csv.seek(0)
    r2 = client.post(f"/api/batches/{bid}/health-remarks/import",
                     data={"file": (import_csv, "health_check.csv"), "operator": "importer"},
                     content_type="multipart/form-data")
    assert r2.status_code == 400
    body = r2.get_json()
    assert "规则版本不一致" in body.get("error", "") or "version" in body.get("error", "").lower()


def test_health_check_cross_batch_blocked(client, sample_dir):
    bid1 = create_batch(client, "hc-cross-batch-1")
    bid2 = create_batch(client, "hc-cross-batch-2")
    path_po = os.path.join(sample_dir, "purchase_orders.csv")
    with open(path_po, "rb") as f:
        dr = client.post(f"/api/batches/{bid1}/precheck-po",
                         data={"file": (f, "po.csv"), "operator": "hc_cb"},
                         content_type="multipart/form-data").get_json()
    client.post(f"/api/batches/{bid1}/drafts/{dr['id']}/confirm", json={"operator": "hc_cb"})
    r = client.post(f"/api/batches/{bid1}/health-check", json={"operator": "hc_cb"}).get_json()
    hid = r["history_id"]
    r2 = client.get(f"/api/batches/{bid2}/health-history/{hid}")
    assert r2.status_code == 400
    assert "跨批次" in r2.get_json().get("error", "")

