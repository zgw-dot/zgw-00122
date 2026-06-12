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
    """确认入账成功，且不暴露 NameError 或 Python 堆栈"""
    bid = create_batch(client, "test-confirm-no-nameerror")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200
    body = r.get_json()
    assert "success" in body, f"match 响应结构异常: {body}"

    resolve_all_exceptions(client, bid)

    # 确认入账——关键路径，之前这里会因 RESULT_STATUS_PENDING 未定义触发 NameError
    r = client.post(f"/api/batches/{bid}/confirm")

    # 1. 返回 JSON，不包含 NameError 或 traceback
    assert r.content_type.startswith("application/json"), f"返回类型不是 JSON: {r.content_type}"
    body = r.get_json()
    body_str = str(body).lower()
    assert "nameerror" not in body_str, f"响应暴露了 NameError: {body}"
    assert "traceback" not in body_str, f"响应暴露了 traceback: {body}"

    # 2. 确认入账成功，状态正确
    assert r.status_code == 200, f"确认入账失败: {body}"
    assert body["status"] == "CONFIRMED", f"确认后状态不对: {body['status']}"

    # 3. 入账接口也正常
    r = client.post(f"/api/batches/{bid}/post")
    assert r.status_code == 200
    assert r.get_json()["status"] == "POSTED"


def test_over_tolerance_single_exception_with_both_sides(client, sample_dir):
    """同供应商同 PO 超容差只生成一条带两边单据、差额、规则版本、备注入口的异常"""
    # 用严格容差触发 OVER_TOLERANCE
    bid = create_batch(client, "test-over-tol", pct=0.5, ab=10)
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    client.post(f"/api/batches/{bid}/match")

    results = get_results(client, bid)
    exceptions = get_exceptions(client, bid)

    over_tol = [r for r in results if r["match_type"] == "OVER_TOLERANCE"]
    unmatched_po = [r for r in results if r["match_type"] == "UNMATCHED_PO"]
    unmatched_inv = [r for r in results if r["match_type"] == "UNMATCHED_INVOICE"]

    # 1. OVER_TOLERANCE 归并记录存在（原来的 bug 是 0 条，拆成 UNMATCHED_PO + UNMATCHED_INVOICE）
    assert len(over_tol) >= 2, f"OVER_TOLERANCE 记录应为 ≥2，实际 {len(over_tol)}"

    # 2. 每条归并记录都含两边单据、差额、规则版本
    for r in over_tol:
        assert r["po_number"], f"记录 {r['id']} 缺 po_number"
        assert r["invoice_number"], f"记录 {r['id']} 缺 invoice_number"
        assert r["po_amount"] is not None, f"记录 {r['id']} 缺 po_amount"
        assert r["invoice_amount"] is not None, f"记录 {r['id']} 缺 invoice_amount"
        assert r["amount_diff"] is not None and r["amount_diff"] > 0, f"记录 {r['id']} 缺 amount_diff"
        assert r["rule_version"], f"记录 {r['id']} 缺 rule_version"
        assert r["is_exception"] is True, f"记录 {r['id']} is_exception 应为 True"

    # 3. 不会拆成两条孤立异常（UNMATCHED_PO/INVOICE 只应各有 1 条，来自 V004/V005）
    assert len(unmatched_po) == 1, f"UNMATCHED_PO 应为 1（V004），实际 {len(unmatched_po)}"
    assert len(unmatched_inv) == 1, f"UNMATCHED_INVOICE 应为 1（V005），实际 {len(unmatched_inv)}"

    # 4. 超容差异常存在，且有备注入口（exceptions 表有对应记录）
    over_ex = [e for e in exceptions if e.get("detail") and "超出容差" in e["detail"]]
    assert len(over_ex) == len(over_tol), f"超容差异常数 {len(over_ex)} 与归并记录数 {len(over_tol)} 不一致"

    # 5. 可以逐条加备注（备注入口正常）
    for e in over_ex:
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/remark", json={"remarks": f"超容差备注{e['id']}"})
        assert r.status_code == 200
        updated = client.get(f"/api/batches/{bid}/exceptions").get_json()["exceptions"]
        target = next(x for x in updated if x["id"] == e["id"])
        assert target["remarks"] == f"超容差备注{e['id']}", "备注写入失败"


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
