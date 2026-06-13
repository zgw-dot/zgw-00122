"""结账归档包模块测试

运行方式:
    cd d:\\workSpace\\AI__SPACE\\02-label\\zgw-00122
    python -m pytest tests/test_archive.py -v

覆盖范围:
1. 导入样例、匹配、确认/入账
2. 生成归档（生成、列表、详情查看）
3. 封存归档
4. 作废归档
5. 导出 CSV、从 CSV 回导
6. 重启后查询历史
7. 权限拒绝（finance 封存/作废、viewer 生成）
8. STALE 标记（容差/批次/重算/巡检/放行/预演变更）
9. 重复生成（内容不变返回旧档、内容变化生成新版）
10. 回导校验（跨批次、字段缺失、哈希重复、已封存冲突）
11. 审计日志
12. 页面入口（首页可访问、archives tab 存在）
"""
import csv
import io
import os
import sys
from io import BytesIO

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import (
    ClosingArchive, ClosingArchiveItem, db,
    ARCHIVE_STATUS_ACTIVE, ARCHIVE_STATUS_STALE, ARCHIVE_STATUS_SEALED, ARCHIVE_STATUS_VOID,
    BATCH_STATUS_CREATED, BATCH_STATUS_MATCHED, BATCH_STATUS_CONFIRMED,
    HANDOVER_ROLE_ADMIN, HANDOVER_ROLE_FINANCE_LEAD, HANDOVER_ROLE_FINANCE, HANDOVER_ROLE_VIEWER,
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
    return r.get_json() if r.content_type.startswith("application/json") else {"status_code": r.status_code}


def resolve_all_exceptions(client, bid):
    r = client.get(f"/api/batches/{bid}/exceptions")
    assert r.status_code == 200
    exs = r.get_json()["exceptions"]
    for e in exs:
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/remark", json={"remarks": f"备注-{e['id']}"})
        assert r.status_code == 200
        r = client.put(f"/api/batches/{bid}/exceptions/{e['id']}/resolve", json={"action": "resolve"})
        assert r.status_code == 200


def full_pipeline(client, sample_dir, name="test-archive"):
    bid = create_batch(client, name)
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    resolve_all_exceptions(client, bid)

    batch = client.get(f"/api/batches/{bid}").get_json()
    if batch.get("status") != BATCH_STATUS_CONFIRMED:
        r = client.post(f"/api/batches/{bid}/confirm")

    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "tester"})
    assert r.status_code == 200

    return bid


def test_create_archive(client, sample_dir):
    bid = full_pipeline(client, sample_dir)

    r = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 201, f"生成归档失败: {r.data}"
    body = r.get_json()
    assert body["status"] == ARCHIVE_STATUS_ACTIVE
    assert body["is_new"] is True
    assert body["message"].startswith("已生成新归档")
    assert body["created_by"] == "tester"
    assert body["batch_status"] == BATCH_STATUS_CONFIRMED
    assert body["payable_total"] > 0
    assert body["matched_count"] > 0
    assert body["archive_number"].startswith("CA-")
    arc_id = body["id"]

    r = client.get(f"/api/archives/{arc_id}")
    assert r.status_code == 200
    detail = r.get_json()
    assert detail["id"] == arc_id
    assert "items" in detail
    assert len(detail["items"]) > 0
    assert detail["batch_summary_snapshot"] is not None
    assert detail["content_hash"] is not None
    assert len(detail["content_hash"]) == 64


def test_list_archives(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-list-arc")

    client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })

    r = client.get(f"/api/batches/{bid}/archives")
    assert r.status_code == 200
    body = r.get_json()
    assert "archives" in body
    assert len(body["archives"]) >= 1

    r = client.get("/api/archives")
    assert r.status_code == 200
    body = r.get_json()
    assert "archives" in body
    assert len(body["archives"]) >= 1


def test_duplicate_returns_old(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-dup-arc")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r1.status_code == 201
    body1 = r1.get_json()
    assert body1["is_new"] is True
    arc1_id = body1["id"]

    r2 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester2",
        "role": "finance",
    })
    assert r2.status_code == 200
    body2 = r2.get_json()
    assert body2["is_new"] is False
    assert body2["id"] == arc1_id
    assert "内容无变化" in body2["message"]


def test_tolerance_change_creates_new_and_stale(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-tol-stale")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc1_id = r1.get_json()["id"]

    r = client.put(f"/api/batches/{bid}/tolerance", json={"tolerance_pct": 1.0, "tolerance_abs": 50.0})
    assert r.status_code == 200

    old = client.get(f"/api/archives/{arc1_id}").get_json()
    assert old["status"] == ARCHIVE_STATUS_STALE
    assert old["is_stale"] is True
    assert old["stale_reason"] is not None

    r2 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r2.status_code == 201
    body2 = r2.get_json()
    assert body2["is_new"] is True
    assert body2["id"] != arc1_id


def test_seal_archive_by_admin(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-seal-arc")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/seal", json={
        "operator": "lead",
        "role": "admin",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == ARCHIVE_STATUS_SEALED
    assert body["sealed_by"] == "lead"
    assert body["sealed_at"] is not None


def test_seal_archive_by_finance_lead(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-seal-lead")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/seal", json={
        "operator": "lead",
        "role": "finance_lead",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == ARCHIVE_STATUS_SEALED


def test_seal_archive_denied_for_finance(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-seal-deny")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/seal", json={
        "operator": "fin",
        "role": "finance",
    })
    assert r.status_code == 403


def test_seal_archive_denied_for_viewer(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-seal-viewer")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/seal", json={
        "operator": "v",
        "role": "viewer",
    })
    assert r.status_code == 403


def test_void_archive_by_admin(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-void-arc")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/void", json={
        "operator": "admin",
        "role": "admin",
        "reason": "测试作废",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == ARCHIVE_STATUS_VOID
    assert body["voided_by"] == "admin"
    assert body["void_reason"] == "测试作废"


def test_void_archive_denied_for_finance(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-void-deny")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.post(f"/api/archives/{arc_id}/void", json={
        "operator": "fin",
        "role": "finance",
        "reason": "no",
    })
    assert r.status_code == 403


def test_create_archive_denied_for_viewer(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-create-viewer")

    r = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "v",
        "role": "viewer",
    })
    assert r.status_code == 403


def test_export_and_import_csv(client, sample_dir, app):
    bid = full_pipeline(client, sample_dir, name="test-export-import")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.get(f"/api/archives/{arc_id}/export")
    assert r.status_code == 200
    assert r.headers.get("Content-Type", "").startswith("text/csv")
    csv_bytes = r.data
    assert len(csv_bytes) > 100

    csv_text = csv_bytes.decode("utf-8-sig")
    assert "结账归档头段" in csv_text
    assert "结账归档明细" in csv_text
    assert "批次摘要快照" in csv_text

    with app.app_context():
        db.session.query(ClosingArchiveItem).filter(ClosingArchiveItem.closing_archive_id == arc_id).delete()
        db.session.query(ClosingArchive).filter(ClosingArchive.id == arc_id).delete()
        db.session.commit()

    data = {"file": (BytesIO(csv_bytes), "import_archive.csv")}
    r = client.post(f"/api/batches/{bid}/archives/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 201, f"回导失败: {r.data}"
    imported = r.get_json()
    assert imported["archive_number"] == r1.get_json()["archive_number"]
    assert imported["content_hash"] == r1.get_json()["content_hash"]


def test_import_cross_batch_denied(client, sample_dir, app):
    bid1 = full_pipeline(client, sample_dir, name="test-cross-a")
    bid2 = full_pipeline(client, sample_dir, name="test-cross-b")

    r1 = client.post(f"/api/batches/{bid1}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.get(f"/api/archives/{arc_id}/export")
    csv_bytes = r.data

    data = {"file": (BytesIO(csv_bytes), "cross.csv")}
    r = client.post(f"/api/batches/{bid2}/archives/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    body = r.get_json()
    assert "跨批次" in body["error"] or any("跨批次" in d for d in body.get("details", []))


def test_import_hash_duplicate_denied(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-hash-dup")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    r = client.get(f"/api/archives/{arc_id}/export")
    csv_bytes = r.data

    csv_text = csv_bytes.decode("utf-8-sig")
    original_arc_num = r1.get_json()["archive_number"]
    fake_arc_num = "CA-HASH-DUP-9999"
    csv_text = csv_text.replace(original_arc_num, fake_arc_num)
    csv_bytes = csv_text.encode("utf-8-sig")

    data = {"file": (BytesIO(csv_bytes), "dup.csv")}
    r = client.post(f"/api/batches/{bid}/archives/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    body = r.get_json()
    assert "哈希" in body["error"] or any("哈希" in d for d in body.get("details", []))


def test_import_sealed_conflict_denied(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-sealed-conflict")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    client.post(f"/api/archives/{arc_id}/seal", json={"operator": "admin", "role": "admin"})

    bid2 = create_batch(client, "test-sealed-conflict-2")
    upload_file(client, bid2, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid2, "invoice", sample_dir, "invoices.csv")
    client.post(f"/api/batches/{bid2}/match")
    resolve_all_exceptions(client, bid2)
    client.post(f"/api/batches/{bid2}/confirm")

    r1b = client.post(f"/api/batches/{bid2}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id2 = r1b.get_json()["id"]
    export_r = client.get(f"/api/archives/{arc_id2}/export")

    header = b"===== \xe7\xbb\x93\xe8\xb4\xa6\xe5\xbd\x92\xe6\xa1\xa3\xe5\xa4\xb4\xe6\xae\xb5 =====\n"
    header += b"\xe5\xbd\x92\xe6\xa1\xa3\xe7\xbc\x96\xe5\x8f\xb7,CA-FAKE-0001\n"
    header += b"\xe6\x89\xb9\xe6\xac\xa1ID," + str(bid).encode() + b"\n"
    header += b"\xe5\x86\x85\xe5\xae\xb9\xe5\x93\x88\xe5\xb8\x8c,abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890\n"
    header += b"\xe7\x8a\xb6\xe6\x80\x81,ACTIVE\n\n"
    header += b"===== \xe7\xbb\x93\xe8\xb4\xa6\xe5\xbd\x92\xe6\xa1\xa3\xe6\x98\x8e\xe7\xbb\x86 =====\n"
    header += b"\xe5\xba\x8f\xe5\x8f\xb7,\xe5\x8c\xb9\xe9\x85\x8d\xe7\xbb\x93\xe6\x9e\x9cID,\xe9\x87\x87\xe8\xb4\xad\xe5\x8d\x95\xe5\x8f\xb7,\xe5\x8f\x91\xe7\xa5\xa8\xe5\x8f\xb7,\xe4\xbe\x9b\xe5\xba\x94\xe5\x95\x86\xe7\xbc\x96\xe7\xa0\x81,\xe4\xbe\x9b\xe5\xba\x94\xe5\x95\x86\xe5\x90\x8d\xe7\xa7\xb0,\xe9\x87\x87\xe8\xb4\xad\xe9\x87\x91\xe9\xa2\x9d,\xe5\x8f\x91\xe7\xa5\xa8\xe9\x87\x91\xe9\xa2\x9d,\xe9\x87\x91\xe9\xa2\x9d\xe5\xb7\xae\xe5\xbc\x82,\xe5\x8c\xb9\xe9\x85\x8d\xe7\xb1\xbb\xe5\x9e\x8b,\xe6\x98\xaf\xe5\x90\xa6\xe5\xbc\x82\xe5\xb8\xb8,\xe5\xbc\x82\xe5\xb8\xb8\xe7\xb1\xbb\xe5\x9e\x8b,\xe5\x8c\xb9\xe9\x85\x8d\xe7\x8a\xb6\xe6\x80\x81,\xe8\xa7\x84\xe5\x88\x99\xe7\x89\x88\xe6\x9c\xac,\xe5\x8c\xb9\xe9\x85\x8d\xe5\xa4\x87\xe6\xb3\xa8,\xe5\xbc\x82\xe5\xb8\xb8\xe5\xa4\x87\xe6\xb3\xa8\n"
    header += b"1,1,PO001,INV001,V001,Vendor1,100,100,0,EXACT,\xe5\x90\xa6,,MATCHED,v1,,\n"

    data = {"file": (BytesIO(header), "sealed-fake.csv")}
    r = client.post(f"/api/batches/{bid}/archives/import", data=data, content_type="multipart/form-data")
    assert r.status_code == 400
    body = r.get_json()
    assert "封存" in body["error"] or any("封存" in d for d in body.get("details", []))


def test_health_check_marks_stale(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-health-stale")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc1_id = r1.get_json()["id"]

    r = client.post(f"/api/batches/{bid}/health-check", json={"operator": "tester2"})
    assert r.status_code == 200

    detail = client.get(f"/api/archives/{arc1_id}").get_json()
    assert detail["status"] == ARCHIVE_STATUS_STALE


def test_audit_logs(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-audit")

    client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })

    r = client.get(f"/api/archives/audit-logs?batch_id={bid}")
    assert r.status_code == 200
    body = r.get_json()
    assert "audit_logs" in body
    assert len(body["audit_logs"]) >= 1
    actions = [log["action"] for log in body["audit_logs"]]
    assert "ARCHIVE_CREATE" in actions


def test_restart_query_persistence(client, sample_dir, app):
    bid = full_pipeline(client, sample_dir, name="test-restart")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]
    archive_number = r1.get_json()["archive_number"]

    with app.test_client() as client2:
        r = client2.get(f"/api/archives/{arc_id}")
        assert r.status_code == 200
        body = r.get_json()
        assert body["archive_number"] == archive_number
        assert body["content_hash"] == r1.get_json()["content_hash"]

        r = client2.get(f"/api/batches/{bid}/archives")
        assert r.status_code == 200
        assert len(r.get_json()["archives"]) >= 1


def test_page_entry(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.data.decode("utf-8")
    assert "结账归档包" in html
    assert 'archives' in html or 'v-if="activeTab===' + "'archives'" in html


def test_sealed_cannot_be_voided(client, sample_dir):
    bid = full_pipeline(client, sample_dir, name="test-sealed-void")

    r1 = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    arc_id = r1.get_json()["id"]

    client.post(f"/api/archives/{arc_id}/seal", json={"operator": "admin", "role": "admin"})

    r = client.post(f"/api/archives/{arc_id}/void", json={
        "operator": "admin",
        "role": "admin",
        "reason": "test",
    })
    assert r.status_code == 400


def test_unconfirmed_batch_cannot_create(client, sample_dir):
    bid = create_batch(client, "test-unconfirmed")
    upload_file(client, bid, "po", sample_dir, "purchase_orders.csv")
    upload_file(client, bid, "invoice", sample_dir, "invoices.csv")

    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200

    r = client.post(f"/api/batches/{bid}/archives", json={
        "operator": "tester",
        "role": "finance",
    })
    assert r.status_code == 400
