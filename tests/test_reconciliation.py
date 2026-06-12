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
