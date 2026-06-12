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
    MatchResult, ExceptionItem, Batch,
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
