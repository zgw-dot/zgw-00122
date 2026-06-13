"""入账放行包模块测试

运行方式:
    cd d:\\workSpace\\AI__SPACE\\02-label\\zgw-00122
    python -m pytest tests/test_release_package.py -v

覆盖范围:
1. 创建批次、导入样例、匹配、巡检
2. 放行包生成（创建、提交、通过、驳回、撤销）
3. 导出 CSV、回导 CSV
4. 重启查询（模拟）
5. 权限拒绝
6. 冲突拒绝（过期包）
7. 审计日志查询
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
    ReleasePackage, ReleasePackageItem,
    RELEASE_STATUS_DRAFT, RELEASE_STATUS_PENDING, RELEASE_STATUS_APPROVED,
    RELEASE_STATUS_REJECTED, RELEASE_STATUS_REVOKED, RELEASE_STATUS_EXPIRED,
    BATCH_STATUS_CREATED, BATCH_STATUS_MATCHED, BATCH_STATUS_CONFIRMED,
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


def resolve_all_exceptions(client, bid):
    r = client.get(f"/api/batches/{bid}/exceptions")
    assert r.status_code == 200
    exs = r.get_json()["exceptions"]
    for e in exs:
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/remark", json={"remarks": f"备注-{e['id']}"})
        assert r.status_code == 200
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/resolve", json={"action": "resolve"})
        assert r.status_code == 200


def full_pipeline(client, sample_dir, name="test-release-pkg"):
    """完整前置：建批次 → 导入样例 → 匹配 → 解决异常（自动确认）→ 巡检"""
    bid = create_batch(client, name)
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    resolve_all_exceptions(client, bid)

    batch = client.get(f"/api/batches/{bid}").get_json()
    if batch.get("status") != BATCH_STATUS_CONFIRMED:
        r = client.post(f"/api/batches/{bid}/confirm")
        if r.status_code != 200:
            body = r.get_json() if r.content_type.startswith("application/json") else {}
            batch = client.get(f"/api/batches/{bid}").get_json()

    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "tester"})
    assert r.status_code == 200

    return bid


# ---------- 测试用例 ----------

def test_create_release_package(client, sample_dir):
    """创建放行包"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "remarks": "测试备注",
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201, f"创建放行包失败: {r.data}"
    body = r.get_json()
    assert body["status"] == RELEASE_STATUS_DRAFT
    assert body["remarks"] == "测试备注"
    assert body["created_by"] == "tester"
    assert body["batch_status"] == BATCH_STATUS_CONFIRMED
    assert body["payable_total"] > 0
    assert "is_new" in body
    pkg_id = body["id"]

    r = client.get(f"/api/release-packages/{pkg_id}")
    assert r.status_code == 200
    detail = r.get_json()
    assert detail["id"] == pkg_id
    assert "items" in detail
    assert len(detail["items"]) > 0


def test_list_release_packages(client, sample_dir):
    """列出放行包"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201

    r = client.get(f"/api/batches/{bid}/release-packages")
    assert r.status_code == 200
    body = r.get_json()
    assert "release_packages" in body
    assert len(body["release_packages"]) >= 1

    r = client.get("/api/release-packages")
    assert r.status_code == 200
    body = r.get_json()
    assert "release_packages" in body
    assert len(body["release_packages"]) >= 1


def test_submit_and_approve_release_package(client, sample_dir):
    """提交审批并通过"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    r = client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    assert r.status_code == 200, f"提交失败: {r.data}"
    assert r.get_json()["status"] == RELEASE_STATUS_PENDING

    r = client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "finance_lead",
        "operator": "leader",
    })
    assert r.status_code == 200, f"通过失败: {r.data}"
    body = r.get_json()
    assert body["status"] == RELEASE_STATUS_APPROVED
    assert body["approved_by"] == "leader"


def test_reject_release_package(client, sample_dir):
    """驳回放行包"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    r = client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    assert r.status_code == 200

    r = client.post(f"/api/release-packages/{pkg_id}/reject", json={
        "reason": "数据不准确",
        "role": "finance_lead",
        "operator": "leader",
    })
    assert r.status_code == 200, f"驳回失败: {r.data}"
    body = r.get_json()
    assert body["status"] == RELEASE_STATUS_REJECTED
    assert body["reject_reason"] == "数据不准确"


def test_revoke_release_package(client, sample_dir):
    """撤销已通过的放行包"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "admin",
        "operator": "admin",
    })

    r = client.post(f"/api/release-packages/{pkg_id}/revoke", json={
        "reason": "发现问题",
        "role": "admin",
        "operator": "admin",
    })
    assert r.status_code == 200, f"撤销失败: {r.data}"
    body = r.get_json()
    assert body["status"] == RELEASE_STATUS_REVOKED
    assert body["revoke_reason"] == "发现问题"


def test_permission_denied_approve(client, sample_dir):
    """权限拒绝：finance 不能审批"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})

    r = client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "finance",
        "operator": "tester",
    })
    assert r.status_code == 403, f"finance 应被禁止审批: {r.data}"


def test_permission_denied_create(client, sample_dir):
    """权限拒绝：viewer 不能创建"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "viewer",
    })
    assert r.status_code == 403, f"viewer 应被禁止创建: {r.data}"


def test_permission_denied_revoke(client, sample_dir):
    """权限拒绝：finance 不能撤销"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "admin",
        "operator": "admin",
    })

    r = client.post(f"/api/release-packages/{pkg_id}/revoke", json={
        "reason": "理由",
        "role": "finance",
        "operator": "tester",
    })
    assert r.status_code == 403, f"finance 应被禁止撤销: {r.data}"


def test_expired_package_approval_blocked(client, sample_dir):
    """过期包：容差变更后旧包过期，审批被拒"""
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    r = client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    assert r.status_code == 200
    assert r.get_json()["status"] == RELEASE_STATUS_PENDING

    client.put(f"/api/batches/{bid}/tolerance", json={
        "tolerance_pct": 5.0,
        "tolerance_abs": 500.0,
    })

    r = client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "finance_lead",
        "operator": "leader",
    })
    assert r.status_code == 400, f"过期包应被拒绝审批: {r.data}"
    body = r.get_json()
    assert "过期" in body.get("error", "") or "expired" in body.get("error", "").lower()

    r = client.get(f"/api/release-packages/{pkg_id}")
    assert r.get_json()["status"] == RELEASE_STATUS_EXPIRED


def test_export_and_reimport_csv(client, sample_dir):
    """导出 CSV 并回导恢复快照"""
    bid = full_pipeline(client, sample_dir, name="test-export-reimport")

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "remarks": "导出测试",
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]
    original_number = r.get_json()["package_number"]

    r = client.get(f"/api/release-packages/{pkg_id}/export")
    assert r.status_code == 200, f"导出失败: {r.data}"
    csv_content = r.data.decode("utf-8-sig")
    assert "===== 放行包头段 =====" in csv_content
    assert "===== 放行包明细 =====" in csv_content
    assert original_number in csv_content

    data = {
        "file": (BytesIO(r.data), f"release_{original_number}.csv"),
        "operator": "importer",
        "role": "finance",
    }
    r = client.post(
        f"/api/batches/{bid}/release-packages/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 400, f"重复回导应被拒绝: {r.data}"
    body = r.get_json()
    assert "重复" in str(body.get("error", "")).lower() or "duplicate" in str(body.get("error", "")).lower()


def test_cross_batch_import_blocked(client, sample_dir):
    """跨批次回导被拒绝"""
    bid1 = full_pipeline(client, sample_dir, name="test-cross-1")
    bid2 = full_pipeline(client, sample_dir, name="test-cross-2")

    r = client.post(f"/api/batches/{bid1}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    r = client.get(f"/api/release-packages/{pkg_id}/export")
    assert r.status_code == 200

    csv_bytes = r.data
    csv_str = csv_bytes.decode("utf-8-sig")
    csv_str_modified = csv_str.replace(f'"{bid1}"', f'"{bid2}"').replace(f',{bid1},', f',{bid2},')
    csv_bytes_modified = csv_str.encode("utf-8-sig")

    data = {
        "file": (BytesIO(csv_bytes_modified), "release_modified.csv"),
        "operator": "importer",
        "role": "finance",
    }
    r = client.post(
        f"/api/batches/{bid2}/release-packages/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code in (400, 403), f"跨批次回导应被拒绝: {r.data}"


def test_missing_fields_import_blocked(client, sample_dir):
    """字段缺失回导被拒绝"""
    bid = full_pipeline(client, sample_dir, name="test-missing-fields")

    bad_csv = "===== 放行包头段 =====\npackage_number,some_field\nRP-001,X\n===== 放行包明细 =====\n"
    data = {
        "file": (BytesIO(bad_csv.encode("utf-8-sig")), "bad.csv"),
        "operator": "importer",
        "role": "finance",
    }
    r = client.post(
        f"/api/batches/{bid}/release-packages/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 400, f"缺字段应被拒绝: {r.data}"
    body = r.get_json()
    assert "缺失" in str(body.get("error", "")) or "缺少" in str(body.get("error", ""))


def test_restart_query(client, sample_dir, app):
    """重启后可查询（模拟：创建包 → 新建客户端查询）"""
    bid = full_pipeline(client, sample_dir, name="test-restart")

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "remarks": "重启测试",
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201
    pkg_id = r.get_json()["id"]

    new_client = app.test_client()

    r = new_client.get(f"/api/release-packages/{pkg_id}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["id"] == pkg_id
    assert body["remarks"] == "重启测试"

    r = new_client.get(f"/api/batches/{bid}/release-packages")
    assert r.status_code == 200
    assert len(r.get_json()["release_packages"]) >= 1


def test_audit_log_query(client, sample_dir):
    """审计日志查询"""
    bid = full_pipeline(client, sample_dir, name="test-audit")

    r = client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })
    pkg_id = r.get_json()["id"]

    client.post(f"/api/release-packages/{pkg_id}/submit", json={"operator": "tester"})
    client.post(f"/api/release-packages/{pkg_id}/approve", json={
        "role": "admin",
        "operator": "admin",
    })

    r = client.get(f"/api/release-packages/audit-logs?package_id={pkg_id}")
    assert r.status_code == 200
    body = r.get_json()
    assert "audit_logs" in body
    assert len(body["audit_logs"]) >= 2

    actions = [l["action"] for l in body["audit_logs"]]
    assert any("CREATE" in a for a in actions)
    assert any("APPROVE" in a for a in actions)


def test_duplicate_same_snapshot(client, sample_dir):
    """同一快照重复创建返回已有包"""
    bid = full_pipeline(client, sample_dir, name="test-dup-snapshot")

    r1 = client.post(f"/api/batches/{bid}/release-packages", json={
        "remarks": "first",
        "operator": "tester",
        "role": "finance",
    })
    assert r1.status_code == 201
    assert r1.get_json()["is_new"] is True
    pkg1_id = r1.get_json()["id"]

    r2 = client.post(f"/api/batches/{bid}/release-packages", json={
        "remarks": "second",
        "operator": "tester",
        "role": "finance",
    })
    assert r2.status_code == 201
    assert r2.get_json()["is_new"] is False
    assert r2.get_json()["id"] == pkg1_id


def test_latest_release_package(client, sample_dir):
    """获取最新放行包"""
    bid = full_pipeline(client, sample_dir, name="test-latest")

    r = client.get(f"/api/batches/{bid}/release-packages/latest")
    assert r.status_code == 200
    assert r.get_json()["release_package"] is None

    client.post(f"/api/batches/{bid}/release-packages", json={
        "operator": "tester",
        "role": "finance",
    })

    r = client.get(f"/api/batches/{bid}/release-packages/latest")
    assert r.status_code == 200
    assert r.get_json()["release_package"] is not None
    assert "items" in r.get_json()["release_package"]
