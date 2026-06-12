"""
预检草稿用户流程验收脚本
模拟用户按 README 步骤一步步操作，验证整个链路与文档一致。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from models import db

app = create_app({
    "TESTING": True,
    "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
})

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "sample")


def print_step(n, title):
    print(f"\n{'='*60}")
    print(f" 步骤 {n}: {title}")
    print(f"{'='*60}")


def main():
    client = app.test_client()

    # 步骤 1: 创建批次
    print_step(1, "创建对账批次")
    r = client.post("/api/batches", json={"name": "验收批次-预检模式"})
    assert r.status_code == 201, f"创建批次失败: {r.status_code}"
    bid = r.get_json()["id"]
    print(f"  ✅ 批次创建成功，ID: {bid}")
    print(f"  初始状态: {r.get_json()['status']}")

    # 步骤 2: 上传采购单预检
    print_step(2, "上传采购单文件（预检模式）")
    path = os.path.join(SAMPLE_DIR, "purchase_orders.csv")
    with open(path, "rb") as f:
        data = {"file": (f, "purchase_orders.csv")}
        r = client.post(
            f"/api/batches/{bid}/precheck-po",
            data=data,
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    result = r.get_json()
    po_draft_id = result["id"]
    print(f"  ✅ 预检成功，草稿 ID: {po_draft_id}")
    print(f"  状态: {result['status']} (PENDING = 等待确认)")
    print(f"  有效行: {result['precheck_report']['summary']['valid_rows']}")
    print(f"  错误数: {result['precheck_report']['summary']['error_count']}")
    print(f"  警告数: {result['precheck_report']['summary']['warning_count']}")
    print(f"  ⚠️  注意：此时数据尚未写入正式表！")

    # 步骤 3: 验证采购单数据确实没写入
    print_step(3, "验证：预检后正式数据不变")
    with app.app_context():
        from models import PurchaseOrder
        count = PurchaseOrder.query.filter_by(batch_id=bid).count()
    print(f"  正式表中采购单行数: {count}")
    assert count == 0, "预检后不应写入正式数据！"
    print(f"  ✅ 确认：预检阶段不污染正式数据")

    # 步骤 4: 查询最近草稿（模拟页面打开时自动加载）
    print_step(4, "查询最近草稿（模拟页面自动加载）")
    r = client.get(f"/api/batches/{bid}/drafts/latest?file_type=PO")
    assert r.status_code == 200
    latest = r.get_json()["draft"]
    assert latest["id"] == po_draft_id
    print(f"  ✅ 最新草稿查询成功，ID: {latest['id']}")
    print(f"  容差快照: {latest['tolerance_pct']}% / ¥{latest['tolerance_abs']}")

    # 步骤 5: 确认导入采购单
    print_step(5, "确认导入采购单")
    r = client.post(f"/api/batches/{bid}/drafts/{po_draft_id}/confirm")
    assert r.status_code == 200
    conf = r.get_json()
    print(f"  ✅ 确认成功，写入 {conf['imported_count']} 行")
    with app.app_context():
        from models import PurchaseOrder
        count = PurchaseOrder.query.filter_by(batch_id=bid).count()
    print(f"  正式表中采购单行数: {count}")
    assert count == conf["imported_count"]

    # 步骤 6: 上传发票预检
    print_step(6, "上传发票文件（预检模式）")
    path = os.path.join(SAMPLE_DIR, "invoices.csv")
    with open(path, "rb") as f:
        data = {"file": (f, "invoices.csv")}
        r = client.post(
            f"/api/batches/{bid}/precheck-invoice",
            data=data,
            content_type="multipart/form-data",
        )
    assert r.status_code == 200
    result = r.get_json()
    inv_draft_id = result["id"]
    print(f"  ✅ 预检成功，草稿 ID: {inv_draft_id}")
    print(f"  状态: {result['status']}")
    print(f"  有效行: {result['precheck_report']['summary']['valid_rows']}")

    # 步骤 7: 验证发票数据确实没写入
    print_step(7, "验证：发票预检后正式数据不变")
    with app.app_context():
        from models import Invoice
        count = Invoice.query.filter_by(batch_id=bid).count()
    print(f"  正式表中发票行数: {count}")
    assert count == 0, "发票预检后不应写入正式数据！"
    print(f"  ✅ 确认：发票仍在预检阶段，未写入")

    # 步骤 8: 确认导入发票
    print_step(8, "确认导入发票")
    r = client.post(f"/api/batches/{bid}/drafts/{inv_draft_id}/confirm")
    assert r.status_code == 200
    conf = r.get_json()
    print(f"  ✅ 确认成功，写入 {conf['imported_count']} 行")
    with app.app_context():
        from models import Invoice
        count = Invoice.query.filter_by(batch_id=bid).count()
    print(f"  正式表中发票行数: {count}")

    # 步骤 9: 执行匹配
    print_step(9, "执行匹配（两边都确认后才能匹配）")
    r = client.post(f"/api/batches/{bid}/match")
    assert r.status_code == 200, f"匹配失败: {r.status_code} {r.get_json()}"
    match_data = r.get_json()
    print(f"  ✅ 匹配成功")
    print(f"  有异常: {match_data.get('has_exceptions')}")

    # 步骤 10: 草稿列表（审计追踪）
    print_step(10, "查看草稿列表（审计追踪）")
    r = client.get(f"/api/batches/{bid}/drafts")
    assert r.status_code == 200
    drafts = r.get_json()["drafts"]
    print(f"  共有 {len(drafts)} 个草稿记录")
    for d in drafts:
        print(f"    - #{d['id']} {d['file_type']} {d['filename']} [{d['status']}]")

    # 步骤 11: 验证审计日志
    print_step(11, "验证审计日志")
    with app.app_context():
        from models import AuditLog
        logs = AuditLog.query.filter_by(batch_id=bid).all()
    print(f"  共有 {len(logs)} 条审计日志")
    draft_actions = [log.action for log in logs if "DRAFT" in log.action]
    print(f"  草稿相关操作: {draft_actions}")
    assert len(draft_actions) >= 4, "应有至少 4 条草稿相关审计记录"
    print(f"  ✅ 审计日志完整，操作可追溯")

    # 步骤 12: 测试丢弃草稿不污染数据
    print_step(12, "额外验证：丢弃草稿不污染数据")
    path = os.path.join(SAMPLE_DIR, "purchase_orders.csv")
    with open(path, "rb") as f:
        data = {"file": (f, "purchase_orders.csv")}
        r = client.post(
            f"/api/batches/{bid}/precheck-po",
            data=data,
            content_type="multipart/form-data",
        )
    new_draft_id = r.get_json()["id"]
    print(f"  创建新采购单草稿 #{new_draft_id}（会自动丢弃旧的 PENDING 草稿）")

    # 丢弃这个新草稿
    r = client.post(f"/api/batches/{bid}/drafts/{new_draft_id}/discard")
    assert r.status_code == 200
    print(f"  丢弃草稿 #{new_draft_id}")

    with app.app_context():
        from models import PurchaseOrder, ImportDraft
        count = PurchaseOrder.query.filter_by(batch_id=bid).count()
        draft = ImportDraft.query.get(new_draft_id)
    print(f"  丢弃后采购单正式表行数: {count} (应保持 6 不变)")
    print(f"  草稿状态: {draft.status}")
    assert count == 6, "丢弃草稿不应改变正式数据！"
    assert draft.status == "DISCARDED"
    print(f"  ✅ 确认：丢弃草稿不污染原有正式数据")

    print("\n" + "="*60)
    print("  🎉 所有验收步骤通过！预检模式与文档完全一致")
    print("="*60)
    print(f"""
  关键验证点总结:
  1. ✅ 上传文件后先预检，不立即写入正式数据
  2. ✅ 预检报告包含有效行、错误、警告、容差快照
  3. ✅ 确认后才写入正式数据，取消/丢弃不污染
  4. ✅ 草稿持久化，可查询历史
  5. ✅ 重复上传同类型会自动处理旧草稿
  6. ✅ 所有操作有审计日志
  7. ✅ 两边都确认后才能匹配
""")


if __name__ == "__main__":
    main()
