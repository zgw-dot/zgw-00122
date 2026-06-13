"""入账预演沙箱模块测试

运行方式:
    cd d:\\workSpace\\AI__SPACE\\02-label\\zgw-00122
    python -m pytest tests/test_rehearsal.py -v

覆盖范围:
1. 导入样例、匹配、确认、巡检
2. 生成预演单（创建、查看）
3. 导出 CSV、回导 CSV
4. 重新生成预演单
5. 重启后查询历史
6. 权限拒绝（viewer 生成、finance 作废）
7. STALE 标记（容差变更、巡检变化）
8. 版本冲突（重复生成返回旧版 vs 新版）
9. 审计日志查询
10. 跨批次回导、字段缺失、哈希重复
11. 页面入口（/ 首页可访问）
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
    RehearsalSlip, RehearsalSlipItem,
    REHEARSAL_STATUS_ACTIVE, REHEARSAL_STATUS_STALE, REHEARSAL_STATUS_VOID,
    BATCH_STATUS_CREATED, BATCH_STATUS_MATCHED, BATCH_STATUS_CONFIRMED,
)


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


def full_pipeline(client, sample_dir, name="test-rehearsal"):
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
            batch = client.get(f"/api/batches/{bid}").get_json()

    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "tester"})
    assert r.status_code == 200

    return bid


def test_create_rehearsal_slip(client, sample_dir):
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201, f"生成预演单失败: {r.data}"
    body = r.get_json()
    assert body["status"] == REHEARSAL_STATUS_ACTIVE
    assert body["is_new"] is True
    assert body["created_by"] == "tester"
    assert body["batch_status"] == BATCH_STATUS_CONFIRMED
    assert body["payable_total"] > 0
    assert body["slip_number"].startswith("RS-")
    slip_id = body["id"]

    r = client.get(f"/api/rehearsal-slips/{slip_id}")
    assert r.status_code == 200
    detail = r.get_json()
    assert detail["id"] == slip_id
    assert "items" in detail
    assert len(detail["items"]) > 0
    assert detail["vendor_payable_summary"] is not None
    assert len(detail["vendor_payable_summary"]) > 0


def test_list_rehearsal_slips(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-list-rehearsal")

    client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })

    r = client.get(f"/api/batches/{bid}/rehearsal-slips")
    assert r.status_code == 200
    body = r.get_json()
    assert "rehearsal_slips" in body
    assert len(body["rehearsal_slips"]) >= 1

    r = client.get("/api/rehearsal-slips")
    assert r.status_code == 200
    body = r.get_json()
    assert "rehearsal_slips" in body
    assert len(body["rehearsal_slips"]) >= 1


def test_latest_rehearsal_slip(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-latest-rehearsal")

    r = client.get(f"/api/batches/{bid}/rehearsal-slips/latest")
    assert r.status_code == 200
    assert r.get_json()["rehearsal_slip"] is None

    client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })

    r = client.get(f"/api/batches/{bid}/rehearsal-slips/latest")
    assert r.status_code == 200
    assert r.get_json()["rehearsal_slip"] is not None
    assert "items" in r.get_json()["rehearsal_slip"]


def test_duplicate_returns_old(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-dup-rehearsal")

    r1 = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r1.status_code == 201
    body1 = r1.get_json()
    assert body1["is_new"] is True
    slip1_id = body1["id"]

    r2 = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester2",
        "role": "finance",
    })
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2["is_new"] is False
    assert body2["id"] == slip1_id


def test_regenerate_creates_new(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-regen-rehearsal")

    r1 = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip1_id = r1.get_json()["id"]

    r2 = client.post(f"/api/batches/{bid}/rehearsal-slips/regenerate", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r2.status_code == 201, f"重新生成失败: {r2.data}"
    body2 = r2.get_json()
    assert body2["is_new"] is True
    assert body2["id"] != slip1_id

    r = client.get(f"/api/batches/{bid}/rehearsal-slips")
    slips = r.get_json()["rehearsal_slips"]
    old_slip = [s for s in slips if s["id"] == slip1_id][0]
    assert old_slip["status"] == REHEARSAL_STATUS_STALE
    assert old_slip["is_stale"] is True


def test_void_rehearsal_slip(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-void-rehearsal")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.post(f"/api/rehearsal-slips/{slip_id}/void", json={
        "reason": "数据有误",
        "role": "admin",
        "operator": "admin",
    })
    assert r.status_code == 200, f"作废失败: {r.data}"
    body = r.get_json()
    assert body["status"] == REHEARSAL_STATUS_VOID
    assert body["voided_by"] == "admin"
    assert body["void_reason"] == "数据有误"


def test_permission_denied_create(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-perm-create")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "viewer_user",
        "role": "viewer",
    })
    assert r.status_code == 403, f"viewer 应被禁止生成: {r.data}"


def test_permission_denied_void(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-perm-void")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.post(f"/api/rehearsal-slips/{slip_id}/void", json={
        "reason": "尝试作废",
        "role": "finance",
        "operator": "finance_user",
    })
    assert r.status_code == 403, f"finance 应被禁止作废: {r.data}"


def test_stale_on_tolerance_change(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-stale-tolerance")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201
    slip_id = r.get_json()["id"]

    client.put(f"/api/batches/{bid}/tolerance", json={
        "tolerance_pct": 5.0,
        "tolerance_abs": 500.0,
    })

    r = client.get(f"/api/rehearsal-slips/{slip_id}")
    assert r.get_json()["status"] == REHEARSAL_STATUS_STALE
    assert r.get_json()["is_stale"] is True
    assert r.get_json()["stale_reason"] is not None


def test_stale_on_health_check(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-stale-health")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201
    slip_id = r.get_json()["id"]

    client.post(f"/api/batches/{bid}/health-check", json={"operator": "tester2"})

    r = client.get(f"/api/rehearsal-slips/{slip_id}")
    assert r.get_json()["status"] == REHEARSAL_STATUS_STALE
    assert r.get_json()["is_stale"] is True


def test_export_and_reimport_csv(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-export-rehearsal")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]
    slip_number = r.get_json()["slip_number"]

    r = client.get(f"/api/rehearsal-slips/{slip_id}/export")
    assert r.status_code == 200, f"导出失败: {r.data}"
    csv_content = r.data.decode("utf-8-sig")
    assert "===== 预演单头段 =====" in csv_content
    assert "===== 预演单明细 =====" in csv_content
    assert slip_number in csv_content

    data = {
        "file": (BytesIO(r.data), f"rehearsal_{slip_number}.csv"),
        "operator": "importer",
    }
    r = client.post(
        f"/api/batches/{bid}/rehearsal-slips/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 400, f"重复回导应被拒绝: {r.data}"
    body = r.get_json()
    assert "重复" in str(body.get("error", "")).lower() or "duplicate" in str(body.get("error", "")).lower() or "已存在" in str(body.get("error", ""))


def test_cross_batch_import_blocked(client, sample_dir):
    bid1 = full_pipeline(client, sample_dir, name="test-cross-rehearsal-1")
    bid2 = full_pipeline(client, sample_dir, name="test-cross-rehearsal-2")

    r = client.post(f"/api/batches/{bid1}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.get(f"/api/rehearsal-slips/{slip_id}/export")
    assert r.status_code == 200

    csv_bytes = r.data

    data = {
        "file": (BytesIO(csv_bytes), "rehearsal_cross.csv"),
        "operator": "importer",
    }
    r = client.post(
        f"/api/batches/{bid2}/rehearsal-slips/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code in (400, 403), f"跨批次回导应被拒绝: {r.data}"


def test_missing_fields_import_blocked(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-missing-rehearsal")

    bad_csv = "===== 预演单头段 =====\nslip_number,some_field\nRS-001,X\n===== 预演单明细 =====\n"
    data = {
        "file": (BytesIO(bad_csv.encode("utf-8-sig")), "bad.csv"),
        "operator": "importer",
    }
    r = client.post(
        f"/api/batches/{bid}/rehearsal-slips/import",
        data=data,
        content_type="multipart/form-data",
    )
    assert r.status_code == 400, f"缺字段应被拒绝: {r.data}"
    body = r.get_json()
    assert "缺失" in str(body.get("error", "")) or "缺少" in str(body.get("error", ""))


def test_restart_query(client, sample_dir, app):
    bid = full_pipeline(client, sample_dir, name="test-restart-rehearsal")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "remarks": "重启测试",
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201
    slip_id = r.get_json()["id"]

    new_client = app.test_client()

    r = new_client.get(f"/api/rehearsal-slips/{slip_id}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["id"] == slip_id

    r = new_client.get(f"/api/batches/{bid}/rehearsal-slips")
    assert r.status_code == 200
    assert len(r.get_json()["rehearsal_slips"]) >= 1


def test_audit_log_query(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-audit-rehearsal")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.get(f"/api/batches/{bid}")
    batch_data = r.get_json()

    r = client.get(f"/api/batches/{bid}")
    assert r.status_code == 200

    r = client.get(f"/api/batches/{bid}")
    assert r.status_code == 200

    from models import AuditLog
    with client.application.app_context():
        logs = AuditLog.query.filter_by(batch_id=bid).filter(AuditLog.action.like("REHEARSAL_%")).all()
        assert len(logs) >= 1
        actions = [l.action for l in logs]
        assert "REHEARSAL_CREATE" in actions


def test_void_double_blocked(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-double-void")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.post(f"/api/rehearsal-slips/{slip_id}/void", json={
        "reason": "第一次作废",
        "role": "admin",
        "operator": "admin",
    })
    assert r.status_code == 200

    r = client.post(f"/api/rehearsal-slips/{slip_id}/void", json={
        "reason": "重复作废",
        "role": "admin",
        "operator": "admin",
    })
    assert r.status_code == 400


def test_version_conflict_regenerate(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-version-conflict")

    r1 = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip1_id = r1.get_json()["id"]

    client.put(f"/api/batches/{bid}/tolerance", json={
        "tolerance_pct": 5.0,
        "tolerance_abs": 500.0,
    })

    r2 = client.post(f"/api/batches/{bid}/rehearsal-slips/regenerate", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r2.status_code == 201
    body2 = r2.get_json()
    assert body2["is_new"] is True
    assert body2["id"] != slip1_id

    r = client.get(f"/api/batches/{bid}/rehearsal-slips")
    slips = r.get_json()["rehearsal_slips"]
    old_slip = [s for s in slips if s["id"] == slip1_id][0]
    assert old_slip["status"] == REHEARSAL_STATUS_STALE
    assert old_slip["is_stale"] is True
    new_slip = [s for s in slips if s["id"] == body2["id"]][0]
    assert new_slip["status"] == REHEARSAL_STATUS_ACTIVE


def test_rehearsal_snapshots(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-snapshots")

    r = client.post(f"/api/batches/{bid}/rehearsal-slips", json={
        "operator": "tester",
        "role": "finance",
    })
    slip_id = r.get_json()["id"]

    r = client.get(f"/api/rehearsal-slips/{slip_id}")
    detail = r.get_json()
    assert detail["vendor_payable_summary"] is not None
    assert detail["exception_result_summary"] is not None
    assert detail["health_inspection_summary"] is not None
    assert detail["recalc_note_version_snapshot"] is not None
    assert detail["batch_status"] == BATCH_STATUS_CONFIRMED


def test_page_entry(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"rehearsal" in r.data or b"rehearsal-slips" in r.data or "预演" in r.data.decode("utf-8")
